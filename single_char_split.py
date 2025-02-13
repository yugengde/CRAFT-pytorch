"""  
Copyright (c) 2019-present NAVER Corp.
MIT License
"""

# -*- coding: utf-8 -*-
import sys
import os
import time
import argparse

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable

from PIL import Image

import cv2
from skimage import io
import numpy as np
import craft_utils
import imgproc
import file_utils
import json
import zipfile

from craft import CRAFT
import math

from collections import OrderedDict
def copyStateDict(state_dict):
    if list(state_dict.keys())[0].startswith("module"):
        start_idx = 1
    else:
        start_idx = 0
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = ".".join(k.split(".")[start_idx:])
        new_state_dict[name] = v
    return new_state_dict

def str2bool(v):
    return v.lower() in ("yes", "y", "true", "t", "1")

parser = argparse.ArgumentParser(description='CRAFT Text Detection')
parser.add_argument('--trained_model', default='weights/craft_mlt_25k.pth', type=str, help='pretrained model')
parser.add_argument('--text_threshold', default=0.7, type=float, help='text confidence threshold')
parser.add_argument('--low_text', default=0.4, type=float, help='text low-bound score')
parser.add_argument('--link_threshold', default=0.4, type=float, help='link confidence threshold')
parser.add_argument('--cuda', default=True, type=str2bool, help='Use cuda to train model')
parser.add_argument('--canvas_size', default=1280, type=int, help='image size for inference')
parser.add_argument('--mag_ratio', default=1.5, type=float, help='image magnification ratio')
parser.add_argument('--poly', default=False, action='store_true', help='enable polygon type')
parser.add_argument('--show_time', default=False, action='store_true', help='show processing time')
parser.add_argument('--test_folder', default='/data/images', type=str, help='folder path to input images')

args = parser.parse_args()


""" For test images in a folder """
image_list, _, _ = file_utils.get_files(args.test_folder)

result_folder = './result/'
if not os.path.isdir(result_folder):
    os.mkdir(result_folder)

def test_net(net, image, text_threshold, link_threshold, low_text, cuda, poly):
    t0 = time.time()

    # resize
    img_resized, target_ratio, size_heatmap = imgproc.resize_aspect_ratio(image, args.canvas_size, interpolation=cv2.INTER_LINEAR, mag_ratio=args.mag_ratio)
    ratio_h = ratio_w = 1 / target_ratio

    # preprocessing
    x = imgproc.normalizeMeanVariance(img_resized)
    x = torch.from_numpy(x).permute(2, 0, 1)    # [h, w, c] to [c, h, w]
    x = Variable(x.unsqueeze(0))                # [c, h, w] to [b, c, h, w]
    if cuda:
        x = x.cuda()

    # forward pass
    y, _ = net(x)

    # make score and link map
    score_text = y[0,:,:,0].cpu().data.numpy()
    score_link = y[0,:,:,1].cpu().data.numpy()

    return score_text


def  vertexCordinate2axisSpan(box):
    box = box.astype(np.int)
    w1, w2 = min(box[:, 0]), max(box[:, 0])
    h1, h2 = min(box[:, 1]), max(box[:, 1])

    return [w1, w2, h1, h2]

def savePartImg(filename, image):
    h, w = image.shape[:2]
    ratio = h / w
    h, w = 28, int(28 * w/h)
    partImg = cv2.resize(image, (w, h), interpolation=cv2.INTER_CUBIC)
    cv2.imwrite(filename, partImg)


def dispalyImg(image):
    cv2.imshow('image', image)
    cv2.waitKey()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    partImgDir = 'charPartImg'
    if not os.path.exists(partImgDir):
        os.mkdir(partImgDir)

    # load net
    net = CRAFT()     # initialize

    print('Loading weights from checkpoint (' + args.trained_model + ')')
    if args.cuda:
        net.load_state_dict(copyStateDict(torch.load(args.trained_model)))
    else:
        net.load_state_dict(copyStateDict(torch.load(args.trained_model, map_location='cpu')))

    if args.cuda:
        net = net.cuda()
        net = torch.nn.DataParallel(net)
        cudnn.benchmark = False

    net.eval()

    t = time.time()

    # load data
    for k, image_path in enumerate(image_list):
        print("Test image {:d}/{:d}: {:s}".format(k+1, len(image_list), image_path), end='\r')
        image = imgproc.loadImage(image_path)
        np_image = np.array(image)

        score_text = test_net(net, image, args.text_threshold, args.link_threshold, args.low_text, args.cuda, args.poly)
        _, char = cv2.threshold(score_text, 0.6, 255, 0)
        char = char.astype(np.uint8)
        contours, _ = cv2.findContours(char, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        coordinates = []
        for k, contour in enumerate(contours):
            x, y, w, h = cv2.boundingRect(contour)
            coor = [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]
            coordinates.append(coor)

        coordinates = np.array(coordinates, np.float64)

        height, width = np_image.shape[:2]
        mag_ratio = args.mag_ratio
        square_size = args.canvas_size
        target_size = mag_ratio * max(height, width)
        if target_size > square_size:
            target_size = square_size
        target_ratio = target_size / max(height, width)    
        ratio_h = ratio_w = 1 / target_ratio

        coordinates = craft_utils.adjustResultCoordinates(coordinates, ratio_w, ratio_h)
        coordinates = coordinates.astype(np.int64)

        a2 = 0.22
        for k, coor in enumerate(coordinates):
            x0, y0 = coor[0]
            x2, y2 = coor[2]
            mi = math.ceil(a2 * math.sqrt((x2-x0) * (y2-y0)))
            if 2 * h < w:
                char_image = image[:, x0-mi:x2+mi, :]
            elif 2 * w < h:
                char_image = image[y0-mi:y2+mi, :, :]
            else:
                char_image = image[y0-mi:y2+mi, x0-mi:x2+mi, :]

            if char_image.size:
                h, w = char_image.shape[:2]
                ratio = h / w
                if ratio > 0.25 and  ratio < 4:
                    filename = os.path.join(partImgDir, "%.10f.jpg" % time.time())
                    savePartImg(filename, char_image)

    print("elapsed time : {}s".format(time.time() - t))

