import os
import sys
import numpy as np
import time
import datetime
import json
import importlib
import logging
import shutil
import asyncio
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from tensorboardX import SummaryWriter
from .model.YOLOv3_PyTorch.nets.model_main import ModelMain
from .model.YOLOv3_PyTorch.nets.yolo_loss import YOLOLoss
from .model.YOLOv3_PyTorch.common.sat_dataset import SatDataset
from .model.YOLOv3_PyTorch.common.utils import non_max_suppression, bbox_iou
from PIL import Image

logger = logging.getLogger(__name__)

class YoloV3TrainActor:
    def __init__(self):
        self.config = self._get_config()
        self.net = self._load_net(self.config)
        self.optimizer = self._get_optimizer(self.config, self.net)
        self.lr_scheduler = optim.lr_scheduler.StepLR(
            self.optimizer,
            step_size=self.config["lr"]["decay_step"],
            gamma=self.config["lr"]["decay_gamma"])
        
        self.checkpoint_path = None
        self.forward_result = {}
        self.model_state_id = 0
        self.criterion = self._get_loss
        self.images = {}
        self.original_size = (1280,720)
    
    def _load_net(self, config):
        net = ModelMain(config, is_training = False)
        net.train(False)

        state_dict = torch.load(self.config["pretrain_snapshot"])
        new_dict = {}
        for key in state_dict:
            new_dict[key[7:]] = state_dict[key]
        net.load_state_dict(new_dict)
        # net = nn.DataParallel(net)
        # net = net.cuda()
        return net
        
    def _get_config(self):
        config = {
            "model_params": {
                "backbone_name": "darknet_53",
                "backbone_pretrained": "" #"trireme/examples/model/YOLOv3_PyTorch/weights/darknet53_weights_pytorch.pth", #  set empty to disable
            },
            "yolo": {
                "anchors": [[[116, 90], [156, 198], [373, 326]],
                            [[30, 61], [62, 45], [59, 119]],
                            [[10, 13], [16, 30], [33, 23]]],
                "classes": 80,
            },
            "lr": {
                "backbone_lr": 0.001,
                "other_lr": 0.01,
                "freeze_backbone": False,   #  freeze backbone wegiths to finetune
                "decay_gamma": 0.1,
                "decay_step": 20,           #  decay lr in every ? epochs
            },
            "optimizer": {
                "type": "sgd",
                "weight_decay": 4e-05,
            },
            "batch_size": 1,
            "epochs": 1,
            "img_h": 416,
            "img_w": 416,
            "parallels": [0,1,2,3],                         #  config GPU device
            "working_dir": "/tmp/sat/",              #  replace with your working dir
            "evaluate_type": "", 
            "try": 0,
            "export_onnx": False,
            "confidence_threshold": 0.98,
            "pretrain_snapshot": "trireme/examples/model/YOLOv3_PyTorch/weights/yolov3_weights_pytorch.pth"
        }
        logging.error("Using config: {}".format(config))
        config["batch_size"] *= len(config["parallels"])

        # Create sub_working_dir
        sub_working_dir = '{}/{}/size{}x{}_try{}/{}'.format(
            config['working_dir'], config['model_params']['backbone_name'], 
            config['img_w'], config['img_h'], config['try'],
            time.strftime("%Y%m%d%H%M%S", time.localtime()))
        if not os.path.exists(sub_working_dir):
            os.makedirs(sub_working_dir)
        config["sub_working_dir"] = sub_working_dir
        logging.info("sub working dir: %s" % sub_working_dir)

        # Creat tf_summary writer
        config["tensorboard_writer"] = SummaryWriter(sub_working_dir)
        logging.info("Please using 'python -m tensorboard.main --logdir={}'".format(sub_working_dir))
        logging.info("Using torch version: {}".format(torch.__version__))

        # Start training
        os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(map(str, config["parallels"]))
        return config
    
    def _get_optimizer(self, config, net):
        optimizer = None

        # Assign different lr for each layer
        params = None
        base_params = list(
            map(id, net.backbone.parameters())
        )
        logits_params = filter(lambda p: id(p) not in base_params, net.parameters())

        if not config["lr"]["freeze_backbone"]:
            params = [
                {"params": logits_params, "lr": config["lr"]["other_lr"]},
                {"params": net.backbone.parameters(), "lr": config["lr"]["backbone_lr"]},
            ]
        else:
            logging.info("freeze backbone's parameters.")
            for p in net.backbone.parameters():
                p.requires_grad = False
            params = [
                {"params": logits_params, "lr": config["lr"]["other_lr"]},
            ]

        # Initialize optimizer class
        if config["optimizer"]["type"] == "adam":
            optimizer = optim.Adam(params, weight_decay=config["optimizer"]["weight_decay"])
        elif config["optimizer"]["type"] == "amsgrad":
            optimizer = optim.Adam(params, weight_decay=config["optimizer"]["weight_decay"],
                                   amsgrad=True)
        elif config["optimizer"]["type"] == "rmsprop":
            optimizer = optim.RMSprop(params, weight_decay=config["optimizer"]["weight_decay"])
        else:
            # Default to sgd
            logging.info("Using SGD optimizer.")
            optimizer = optim.SGD(params, momentum=0.9,
                                  weight_decay=config["optimizer"]["weight_decay"],
                                  nesterov=(config["optimizer"]["type"] == "nesterov"))

        return optimizer
    
    def _get_loss(self, outputs, labels):
        yolo_losses = []
        for i in range(3):
            yolo_losses.append(YOLOLoss(self.config["yolo"]["anchors"][i],
                                        self.config["yolo"]["classes"], (self.config["img_w"], self.config["img_h"])))
        losses_name = ["total_loss", "x", "y", "w", "h", "conf", "cls"]
        losses = [[]] * len(losses_name)
        
        for i in range(3):
            # print(outputs.shape)  #should be (3, 1, 255, 13, 13), is (1, 1, 288, 13, 13)
            _loss_item = yolo_losses[i](outputs[i], labels)
            for j, l in enumerate(_loss_item):
                losses[j].append(l)
        losses = [sum(l) for l in losses]
        loss = losses[0] 
        return loss   
        
    def _save_checkpoint(state_dict, config, evaluate_func=None):
        # global best_eval_result
        checkpoint_path = os.path.join(config["sub_working_dir"], "model.pth")
        torch.save(state_dict, checkpoint_path)
        logging.info("Model checkpoint saved to %s" % checkpoint_path)
        return checkpoint_path
            
    async def __call__(self, input_batch):
        # Routing logic
        # There will be four batches
        cancel_batch = []
        re_forward_batch = []
        backward_batch = []
        new_forward_batch = []

        for inp in input_batch:
            if inp["path"] == "infer":
                new_forward_batch.append(inp)

            elif inp["path"] == "train":
                if int(inp["model state id"]) != self.model_state_id:
                    re_forward_batch.append(inp)
                backward_batch.append(inp)

            elif inp["path"] == "cancel":
                if int(inp["model state id"]) == self.model_state_id:
                    cancel_batch.append(inp)

        batch_info = f"""
        Cancel {len(cancel_batch)};
        Re-forward {len(re_forward_batch)};
        Backward {len(backward_batch)};
        Forward {len(new_forward_batch)}"""
        logger.info(f"YOLO NN is processing {batch_info}")

        self._handle_cancel(cancel_batch)
        self._handle_forward(re_forward_batch)
        await asyncio.sleep(0)
        self._handle_backward(backward_batch)
        await asyncio.sleep(0)
        return self._handle_forward(new_forward_batch)

    def _handle_cancel(self, input_batch):
        for inp in input_batch:
            self.forward_result.pop(inp["object id"])

    def _handle_backward(self, input_batch):
        self.config["global_step"] = self.config.get("start_step", 0)
        
        imgs = []
        targets = []
        for inp in input_batch:
            img = self.images[inp["object id"]]
            imgs.append(img)
            label = inp["label"]
            labels = []
            for entry in label:
                bbox = entry["bbox"]
                bbox.insert(0, entry["category_id"])
                labels.append([5, 0.5, 0.5, 0.2, 0.2])
            targets.append(labels)
        

        # DataLoader
        dataloader = torch.utils.data.DataLoader(SatDataset(imgs, targets,
                                                             (self.config["img_w"], self.config["img_h"]),
                                                             is_training=True),
                                                 batch_size=self.config["batch_size"],
                                                 shuffle=True, num_workers=0, pin_memory=True)
                                                 
        # Start the training loop
        for epoch in range(self.config["epochs"]):
            for step, samples in enumerate(dataloader):
                images, labels = samples["image"], samples["label"]
                start_time = time.time()
                self.config["global_step"] += 1

                # Forward and backward
                self.optimizer.zero_grad()
                outputs = self.net(images)
                loss = self.criterion(outputs, labels)
                loss.backward()
                self.optimizer.step()

            self.lr_scheduler.step()

        self.model_state_id += 1
        self.forward_result = {}
        self.images = {}

    def _handle_forward(self, input_batch):
        if len(input_batch) == 0:
            return []

        # Convert to tensors
        tensors = []
        self.np_inputs = []
        for inp in input_batch:
            pil_image = inp["input"]
            pil_image = pil_image.resize((416,416), Image.ANTIALIAS)
            # The following input processing step can go into:
            # - downloader
            # - another middleware
            # - trainer
            np_input = np.array(pil_image, dtype=np.float32).reshape(3, 416, 416)
            self.np_inputs.append(np_input.transpose())
            tensor = torch.Tensor(np_input)
            tensor = tensor.unsqueeze(0)
            tensors.append(tensor)
        batch_inp = torch.cat(tensors)

        forward_res = self.net(batch_inp)
        return self._output_to_bbox(forward_res, input_batch)

    def _output_to_bbox(self, outputs, input_batch):
        yolo_losses = []
        for i in range(3):
            yolo_losses.append(YOLOLoss(self.config["yolo"]["anchors"][i],
                                        self.config["yolo"]["classes"], (self.config["img_w"], self.config["img_h"])))

        returns = []
        with torch.no_grad():
            output_list = []
            for i in range(3):
                output_list.append(yolo_losses[i](outputs[i]))
            output = torch.cat(output_list, 1)
            output = non_max_suppression(output,
                                         self.config["yolo"]["classes"],
                                         conf_thres=self.config["confidence_threshold"],
                                         nms_thres=0.85)
            logger.info(len(output))
            ori_w, ori_h = self.original_size
            pre_w, pre_h = self.config["img_w"], self.config["img_h"]

            for i, inp in enumerate(input_batch):
                self.forward_result[inp["object id"]] = outputs[i]
                self.images[inp["object id"]] = self.np_inputs[i]

                sample_pred = output[i]

                results = []
                if sample_pred is not None:
                    for x1, y1, x2, y2, conf, obj_conf, obj_pred in sample_pred:
                        y1 = (y1.item() / pre_h) * ori_h
                        x1 = (x1.item() / pre_w) * ori_w
                        y2 = (y2.item() / pre_h) * ori_h
                        x2 = (x2.item() / pre_w) * ori_w
                        pred = {
                                    "bbox": [x1, y1, x2, y2],
                                    "cls": obj_pred.item()
                                }
                        results.append(pred)
                r = {
                    "prediction": results,
                    "object id": inp["object id"],
                    "model state id": self.model_state_id
                }
                returns.append(r)
        return returns


