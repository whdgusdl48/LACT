import os
import argparse
import functools
import tensorflow as tf
import numpy as np
import time

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

from lib.model import build_vgg16
from lib.model import build_luminance_transform_function,post_processing_module, LuminanceAttention
from lib.model import lightnet_v6, lightnet_v6_new
from lib.multi_datasets import build_dataset_multi2 as build_dataset
from lib.utils import psnr, ssim, WarmupCosineSchedule
from tqdm import tqdm

gpus = tf.config.experimental.list_physical_devices('GPU')
tf.config.experimental.set_visible_devices(gpus[0], 'GPU')

parser = argparse.ArgumentParser()
parser.add_argument("--title", type=str, default="")
parser.add_argument("--dataset", type=str, default="")
parser.add_argument("--train_db", type=str,
                    default="")
parser.add_argument("--test_db", type=str,
                    default="")
parser.add_argument("--batch_size", type=int, default=2)
parser.add_argument("--epochs", type=int, default=20)
parser.add_argument("--learning_rate", type=float, default=3e-4)
parser.add_argument("--warmup_steps", type=int, default=8765)
parser.add_argument("--decay_steps", type=int, default=432150)
parser.add_argument("--log_dir", type=str, default="result")

args = parser.parse_args()
VGG16 = build_vgg16()

def compute_loss(targets, outputs, scale_p=5e-2, scale_tv=1e-2,scale_fft=5e-1):

    loss_c = color_loss(targets,outputs)
    loss_p = perceptual_loss(targets,outputs)
    loss_fft = fft_loss(targets,outputs)
    loss = loss_c + scale_fft * loss_fft + scale_p * loss_p

    return loss

def image_to_freq(image):
    freq = tf.signal.fft2d(tf.cast(image,tf.complex64))
    freq = tf.stack([tf.math.real(freq),tf.math.imag(freq)],-1)
    return freq

def fft_loss(targets,outputs):
    targets = image_to_freq(targets)
    outputs = image_to_freq(outputs)
    loss = tf.losses.MeanAbsoluteError()(targets,outputs)
    return loss

def color_loss(targets,outputs):
    return tf.losses.MeanAbsoluteError()(targets,outputs)

def perceptual_loss(targets,outputs):
    z1, z2, z3 = VGG16(targets, training=False)
    z_hat1, z_hat2, z_hat3 = VGG16(outputs, training=False)
    loss_p = tf.keras.losses.MeanAbsoluteError()(z1, z_hat1) \
             + tf.keras.losses.MeanAbsoluteError()(z2, z_hat2) \
             + tf.keras.losses.MeanAbsoluteError()(z3, z_hat3)
    return loss_p

class gen_light(tf.keras.Model):
    def __init__(self):
        super(gen_light,self).__init__()
        self.light_net = lightnet_v6_new()
        self.attention = LuminanceAttention(3, 256)
        
        self.global_net = build_luminance_transform_function(self.light_net, self.attention)
        self.local_net = post_processing_module()
        self.con = tf.keras.layers.Concatenate()
        self.fc = tf.keras.layers.Dense(1, activation='sigmoid')

        self.global_conv = tf.keras.layers.Conv2D(3, 3, 1,padding='same')
        self.act = tf.keras.layers.Activation('tanh')


    def __call__(self, inputs, training=False):
        if training:
            x1,x2 = tf.split(inputs,2,axis=1)
            x1 = tf.squeeze(x1,axis=1)
            x2 = tf.squeeze(x2,axis=1)
            g1_feature,f1 = self.global_net(x1)
            g1 = self.global_conv(g1_feature)
            g1 = self.act(g1)
            y1 = self.local_net(g1_feature)

            g2_feature,f2 = self.global_net(x2)
            g2 = self.global_conv(g2_feature)
            g2 = self.act(g2)
            y2 = self.local_net(g2_feature)

            con = self.con([f1,f2])
            fc = self.fc(con)
            return g1,g2,y1,y2,fc

        else:
            g1_feature,f1 = self.global_net(inputs)
            g1 = self.global_conv(g1_feature)
            g1 = self.act(g1)
            y1 = self.local_net(g1_feature)

            return y1,g1


def train_step(data, label, model, optimizer,alpha, epoch):
    label_1, label_2 = tf.split(label, 2, axis=1)
    label_1 = tf.squeeze(label_1, axis=1)
    label_2 = tf.squeeze(label_2, axis=1)
    with tf.GradientTape() as tape:
        g1,g2,y1, y2, fc = model(data,training=True)

        loss1 = compute_loss(label_1, y1)
        loss2 = compute_loss(label_2,y2)
        loss3 = tf.keras.losses.binary_crossentropy(alpha, fc)
        loss4 = compute_loss(label_1, g1)
        loss5 = compute_loss(label_2, g2)
        loss = loss1  + loss2  + loss3 + loss4+ loss5

    optimizer.minimize(loss, model.trainable_variables, tape=tape)
    summary = {'psnr': psnr(label_1, y1), 'ssim': ssim(label_1, y1),'psnr_g': psnr(label_1, g1), 'ssim_g': ssim(label_1, g1)}
    acc = tf.keras.metrics.BinaryAccuracy()
    accuracy = acc(alpha,fc)
    return summary,accuracy, loss

def test_step(data, label, model):
    y1,g1 = model(data,training=False)
    summary = {'psnr': psnr(label, y1), 'ssim': ssim(label, y1),'g_psnr':psnr(label,g1),'g_ssim':ssim(label,g1)}
    return y1,summary

def main():
    import datetime
    now = datetime.datetime.now()
    now = now.strftime('%Y_%m_%d_%H_%M_%S')

    os.makedirs(args.log_dir, exist_ok=True)

    today_date = datetime.datetime.now().strftime("%m%d")
    if not args.title+"_" + today_date in os.listdir("./log"):
        os.mkdir("./log/"+args.title+"_" + today_date)

    # dataset
    train_ds = build_dataset(args.dataset, args.train_db, args.batch_size, training=True)
    test_ds = build_dataset(args.dataset, args.test_db, 1,training=False)

    checkpoint_path = ""
    model = gen_light()
    checkpoint = tf.train.Checkpoint(model)
    checkpoint.restore(checkpoint_path)
    schedules = WarmupCosineSchedule(args.learning_rate, args.warmup_steps, args.decay_steps)
    optimizer = tf.keras.optimizers.RMSprop(schedules)

    idx = 0

    for data, label in test_ds:
        inf,test_summary = test_step(data, label, model)
        prediction = tf.cast(tf.clip_by_value(0.5 * (inf + 1.0), 0.0, 1.0), 'float32')
        io.imsave(""+str(idx)+".png",prediction[0].numpy())
        idx += 1

if __name__ == '__main__':
    main()
