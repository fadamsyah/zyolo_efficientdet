# Source
# https://github.com/shiyinzhang/Inside-Outside-Guidance/blob/master/test.py

import numpy as np
import cv2
import torch
from copy import deepcopy
from torchvision import transforms
from torch.utils.data import Dataset, DataLoader
from .dataloaders import custom_transforms as tr
from .dataloaders.helpers import *
from .networks.mainnetwork import *

class IoGDataset(Dataset):
    def __init__(self, image, annotations, transforms):
        self.image = image
        self.annotations = annotations
        self.transforms = transforms
        
    def __len__(self):
        return len(self.annotations)
    
    def __getitem__(self, idx):
        annot = self.annotations[idx]
        
        xmin, ymin, w, h = list(map(int, annot))
        xmax, ymax = xmin + w, ymin + h
        
        # Make a masking
        bbox = np.zeros_like(self.image[..., 0])
        bbox[ymin:ymax, xmin:xmax] = 1
        void_pixels = 1 - bbox
        
        iog_input = {'image': self.image, 'gt': bbox, 'void_pixels': void_pixels}
        iog_input = self.transforms(iog_input)
        
        return iog_input
    
    def change_image(self, image, annotations):
        self.image = image
        self.annotations = annotations

# TODO
# Adapt to multiclass segmentation
class IoGNetwork(object):
    def __init__(self, nInputChannels=5, num_classes=1, backbone='resnet101',
                 output_stride=16, sync_bn=None, freeze_bn=False,
                 pretrain_path='models/IOG_PASCAL_SBD.pth', use_cuda=False,
                 interpolation=cv2.INTER_LINEAR, threshold=None):
        
        # Threshold
        self.threshold = threshold
        
        # Initialize the  network
        self.net = Network(nInputChannels=nInputChannels,
                           num_classes=num_classes,
                           backbone=backbone,
                           output_stride=output_stride,
                           sync_bn=sync_bn,
                           freeze_bn=freeze_bn)
        
        # Load the pretrained model
        print(f"Initializing weights from {pretrain_path}")
        pretrain_dict = torch.load(pretrain_path)       
        self.net.load_state_dict(pretrain_dict)
        
        # Setting the model to the evaluation mode
        self.net.eval()
        
        # use_cuda
        self.use_cuda = use_cuda
        if self.use_cuda: self.net = self.net.cuda()
        
        # Define the transformations
        self.transforms = transforms.Compose([
            tr.CropFromMask(crop_elems=('image', 'gt','void_pixels'), relax=30, zero_pad=True),
            tr.FixedResize(resolutions={'gt': None,
                                        'crop_image': (512, 512),
                                        'crop_gt': (512, 512),
                                        'crop_void_pixels': (512, 512)},
                           flagvals={'gt' : interpolation,
                                     'crop_image' : interpolation,
                                     'crop_gt' : interpolation,
                                     'crop_void_pixels': interpolation}),
            tr.IOGPoints(sigma=10, elem='crop_gt',pad_pixel=10),
            tr.ToImage(norm_elem='IOG_points'),
            tr.ConcatInputs(elems=('crop_image', 'IOG_points')),
            tr.ToTensor()])
        
        self.iog_dataset = IoGDataset(None, None, self.transforms)
        
    def __call__(self, img_path, annotations):
        img = cv2.imread(img_path)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)
        
        if len(annotations["analysis_results"]) == 0:
            return np.zeros_like(img[..., 0])
        
        annots = list(map(self.__read_annotations, annotations["analysis_results"]))
        
        self.iog_dataset.change_image(img, annots)
        samples = []
        for idx in range(len(self.iog_dataset)):
            samples.append(self.iog_dataset[idx])
                
        with torch.no_grad():
            inputs = torch.stack([sample['concat'] for sample in samples], 0)
            if self.use_cuda: inputs = inputs.cuda()
            outputs = self.net.forward(inputs)[-1]
            preds = np.stack([np.transpose(output, (1,2,0)) for output in outputs.data.cpu().numpy()], 0)
            preds = 1. / (1. + np.exp(-preds))
            preds = preds[..., 0]
            results = np.zeros_like(img[..., 0])
            for pred, sample in zip(preds, samples):
                gt = tens2image(sample['gt'])
                bbox = get_bbox(gt, pad=30, zero_pad=True)
                results = results + crop2fullmask(pred, bbox, gt, zero_pad=True, relax=0,mask_relax=False) 
                results[results > 1.] = 1.
        
        if self.threshold is not None:
            results[results < self.threshold] = 0.
        
        return results
                    
    @staticmethod
    def __read_annotations(obj):
        bbox = obj['bbox']
        return [bbox["xmin"], bbox["ymin"],
                bbox["xmax"] - bbox["xmin"],
                bbox["ymax"] - bbox["ymin"]]