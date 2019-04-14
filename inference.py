# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from ._init_paths import *
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time
import cv2
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim

import torchvision.transforms as transforms
import torchvision.datasets as dset
from scipy.misc import imread
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
# from model.nms.nms_wrapper import nms
from model.roi_layers import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections
from model.utils.blob import im_list_to_blob
from model.faster_rcnn.vgg16 import vgg16
from model.faster_rcnn.resnet import resnet
import pdb

try:
    xrange          # Python 2
except NameError:
    xrange = range  # Python 3


def parse_args():
  """
  Parse input arguments
  """
  parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
  parser.add_argument('--dataset', dest='dataset',
                      help='training dataset',
                      default='pascal_voc', type=str)
  parser.add_argument('--cfg', dest='cfg_file',
                      help='optional config file',
                      default='cfgs/vgg16.yml', type=str)
  parser.add_argument('--net', dest='net',
                      help='vgg16, res50, res101, res152',
                      default='res101', type=str)
  parser.add_argument('--set', dest='set_cfgs',
                      help='set config keys', default=None,
                      nargs=argparse.REMAINDER)
  parser.add_argument('--load_dir', dest='load_dir',
                      help='directory to load models',
                      default="/srv/share/jyang375/models")
  parser.add_argument('--image_dir', dest='image_dir',
                      help='directory to load images for demo',
                      default="images")
  parser.add_argument('--cuda', dest='cuda',
                      help='whether use CUDA',
                      action='store_true')
  parser.add_argument('--mGPUs', dest='mGPUs',
                      help='whether use multiple GPUs',
                      action='store_true')
  parser.add_argument('--cag', dest='class_agnostic',
                      help='whether perform class_agnostic bbox regression',
                      action='store_true')
  parser.add_argument('--parallel_type', dest='parallel_type',
                      help='which part of model to parallel, 0: all, 1: model before roi pooling',
                      default=0, type=int)
  parser.add_argument('--checksession', dest='checksession',
                      help='checksession to load model',
                      default=1, type=int)
  parser.add_argument('--checkepoch', dest='checkepoch',
                      help='checkepoch to load network',
                      default=1, type=int)
  parser.add_argument('--checkpoint', dest='checkpoint',
                      help='checkpoint to load network',
                      default=10021, type=int)
  parser.add_argument('--bs', dest='batch_size',
                      help='batch_size',
                      default=1, type=int)
  parser.add_argument('--vis', dest='vis',
                      help='visualization mode',
                      action='store_true')
  parser.add_argument('--webcam_num', dest='webcam_num',
                      help='webcam ID number',
                      default=-1, type=int)

  args = parser.parse_args()
  return args

lr = cfg.TRAIN.LEARNING_RATE
momentum = cfg.TRAIN.MOMENTUM
weight_decay = cfg.TRAIN.WEIGHT_DECAY

def _get_image_blob(im):
  """Converts an image into a network input.
  Arguments:
    im (ndarray): a color image in BGR order
  Returns:
    blob (ndarray): a data blob holding an image pyramid
    im_scale_factors (list): list of image scales (relative to im) used
      in the image pyramid
  """
  im_orig = im.astype(np.float32, copy=True)
  im_orig -= cfg.PIXEL_MEANS

  im_shape = im_orig.shape
  im_size_min = np.min(im_shape[0:2])
  im_size_max = np.max(im_shape[0:2])

  processed_ims = []
  im_scale_factors = []

  for target_size in cfg.TEST.SCALES:
    im_scale = float(target_size) / float(im_size_min)
    # Prevent the biggest axis from being more than MAX_SIZE
    if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
      im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
    im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
            interpolation=cv2.INTER_LINEAR)
    im_scale_factors.append(im_scale)
    processed_ims.append(im)

  # Create a blob to hold the input images
  blob = im_list_to_blob(processed_ims)

  return blob, np.array(im_scale_factors)

class FRCnnWrapper():

  def __init__(self):
      self.cuda = 1
      cfg.USE_GPU_NMS = self.cuda
      self.input_dir = os.path.dirname(os.path.realpath(__file__)) + "/models/res101/df"
      np.random.seed(cfg.RNG_SEED)
      self.load_name = os.path.join(self.input_dir,
      'faster_rcnn_{}_{}_{}.pth'.format(1, 4, 26151))
      self.pascal_classes = np.asarray(['__background__',
                        'top',
                        'bottom',
                        'full'])
      fasterRCNN = resnet(self.pascal_classes, 101, pretrained=False, class_agnostic=False)
      fasterRCNN.create_architecture()
      print("load checkpoint %s" % (self.load_name))
      checkpoint = torch.load(self.load_name)
      if 'pooling_mode' in checkpoint.keys():
        cfg.POOLING_MODE = checkpoint['pooling_mode']
      fasterRCNN.load_state_dict(checkpoint['model'])
      print('load model successfully!')
      self.fasterRCNN = fasterRCNN

      if self.cuda > 0:
        self.fasterRCNN.cuda()

      if self.cuda > 0:
        cfg.CUDA = True

      self.fasterRCNN.eval()

  def inference(self,imgs):
      # images should come from 
      #im_in = np.array(imread(im_file))

      # initilize the tensor holder here.
      im_data = torch.FloatTensor(1)
      im_info = torch.FloatTensor(1)
      num_boxes = torch.LongTensor(1)
      gt_boxes = torch.FloatTensor(1)

      # ship to cuda
      if self.cuda > 0:
        im_data = im_data.cuda()
        im_info = im_info.cuda()
        num_boxes = num_boxes.cuda()
        gt_boxes = gt_boxes.cuda()

      # make variable
      im_data = Variable(im_data, volatile=True)
      im_info = Variable(im_info, volatile=True)
      num_boxes = Variable(num_boxes, volatile=True)
      gt_boxes = Variable(gt_boxes, volatile=True)

      
      max_per_image = 100
      thresh = 0.05
      num_images = len(imgs)
      cls_array = []
      bbox_array = []
      scores_array = []
      while (num_images >= 0):
        num_images -= 1
        im_in = imgs[num_images]
        if len(im_in.shape) == 2:
          im_in = im_in[:,:,np.newaxis]
          im_in = np.concatenate((im_in,im_in,im_in), axis=2)
        # rgb -> bgr
        im = im_in[:,:,::-1]
        blobs, im_scales = _get_image_blob(im)
        assert len(im_scales) == 1, "Only single-image batch implemented"
        im_blob = blobs
        im_info_np = np.array([[im_blob.shape[1], im_blob.shape[2], im_scales[0]]], dtype=np.float32)

        im_data_pt = torch.from_numpy(im_blob)
        im_data_pt = im_data_pt.permute(0, 3, 1, 2)
        im_info_pt = torch.from_numpy(im_info_np)

        im_data.data.resize_(im_data_pt.size()).copy_(im_data_pt)
        im_info.data.resize_(im_info_pt.size()).copy_(im_info_pt)
        gt_boxes.data.resize_(1, 1, 5).zero_()
        num_boxes.data.resize_(1).zero_()
        rois, cls_prob, bbox_pred, \
        rpn_loss_cls, rpn_loss_box, \
        RCNN_loss_cls, RCNN_loss_bbox, \
        rois_label = self.fasterRCNN(im_data, im_info, gt_boxes, num_boxes)

        scores = cls_prob.data
        boxes = rois.data[:, :, 1:5]
        
        if cfg.TEST.BBOX_REG:
          # Apply bounding-box regression deltas
          box_deltas = bbox_pred.data
          if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
          # Optionally normalize targets by a precomputed mean and stdev
            if self.cuda > 0:
                box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                            + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
            else:
                box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS) \
                            + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS)
            box_deltas = box_deltas.view(1, -1, 4 * len(self.pascal_classes))

          pred_boxes = bbox_transform_inv(boxes, box_deltas, 1)
          pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
        else:
          # Simply repeat the boxes, once for each class
          pred_boxes = np.tile(boxes, (1, scores.shape[1]))

        pred_boxes /= im_scales[0]

        scores = scores.squeeze()
        pred_boxes = pred_boxes.squeeze()
        classes_img = []
        bboxes_img =  []
        scores_img  = []
        for j in range(1, len(self.pascal_classes)):
            inds = torch.nonzero(scores[:,j]>thresh).view(-1)
            # if there is det
            if inds.numel() > 0:
              cls_scores = scores[:,j][inds]
              _, order = torch.sort(cls_scores, 0, True)
              cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4]
              cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1)
              # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
              cls_dets = cls_dets[order]
              # keep = nms(cls_dets, cfg.TEST.NMS, force_cpu=not cfg.USE_GPU_NMS)
              keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS)
              cls_dets = cls_dets[keep.view(-1).long()]
              cls_dets_cpu = cls_dets.cpu().numpy()
              for i in range(np.minimum(10, cls_dets_cpu.shape[0])):
                score = cls_dets_cpu[i, -1]
                if score > 0.5: # inference threshhold
                  bbox = tuple(int(np.round(x)) for x in cls_dets_cpu[i, :4])
                  classes_img.append(self.pascal_classes[j])
                  bboxes_img.append(bbox)
                  scores_img.append(score)
        cls_array.append(classes_img)
        scores_array.append(scores_img)
        bbox_array.append(bboxes_img)
        print(cls_array)
        print(scores_array)
        print(bbox_array)

      return cls_array, scores_array, bbox_array
if __name__ == '__main__':
  cnn = FRCnnWrapper()
  imglist = os.listdir('images')
  images = [np.array(imread(os.path.join('images',im_file))) for im_file in imglist]
  print(cnn.inference(images))
