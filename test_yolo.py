"""
This demo work as follows:
- infer_request_producer will start sending a inference request every 0-2 seconds
- infer_response_consumer will listen to the websocket and receive prediction. If the prediction 
  matches our label, it will put a cancellation request in the queue; else, it will put a training
  request with the correct label in the queue.
- last_request_handler will consume the third request queue
"""

import asyncio
import websockets
import torch
from torchvision import transforms, datasets
import json
import numpy as np
import redis
import hashlib
import sys
import time

feedback_queue = asyncio.Queue(maxsize=10000)
oid_to_label = {}

r = redis.Redis()

oid_start_time = {}

SLEEP_TIME = 10
if len(sys.argv) > 1:
    SLEEP_TIME = float(sys.argv[1])

class YoloGenInputActor:
    def __init__(self):
        yolo_trans = transforms.Compose(
            [transforms.Resize((416, 416)), transforms.ToTensor()]
        )
        self.data = datasets.CocoDetection(
            root="data/resized_val2017", annFile="data/annotations/instances_val2017.json", transform=yolo_trans
        )
        self.data_iter = iter(self.data)
        print("YoloGenInputActor init")

    def _get_inp(self):
        try:
            return next(self.data_iter)
        except StopIteration:
            self.data_iter = iter(self.data)
            return next(self.data_iter)

    def __call__(self):
        next_inp = self._get_inp()

        inp_numpy = next_inp[0].detach().numpy()
        inp_bytes = inp_numpy.tobytes()

        md5_hash = hashlib.sha1(inp_bytes).hexdigest()

        r.set(md5_hash, inp_bytes)
        oid = "redis://" + md5_hash

        wrapped = {"object id": oid, "label": next_inp[1]}

        return wrapped


async def infer_request_producer(websocket):
    generator = YoloGenInputActor()

    while True:
        inp = generator()
        pred_req = {"path": "infer", "object id": inp["object id"]}
        serialized = json.dumps(pred_req)
        print(serialized)

        oid_start_time[inp["object id"]] = time.time()
        await websocket.send(serialized)
        oid_to_label[inp["object id"]] = inp["label"]

        await asyncio.sleep(SLEEP_TIME)


async def infer_response_consumer(websocket):
    while True:
        resp = await websocket.recv()
        resp_dict = json.loads(resp)
        inp_oid = resp_dict["object id"]

        print(f"Prediction {inp_oid} took {time.time() - oid_start_time[inp_oid]} seconds")

        pred = resp_dict["prediction"]
        label = oid_to_label[inp_oid]

        if pred == label:
            await feedback_queue.put(
                {
                    "path": "cancel",
                    "model state id": resp_dict["model state id"],
                    "object id": inp_oid,
                }
            )
        else:
            await feedback_queue.put(
                {
                    "path": "train",
                    "model state id": resp_dict["model state id"],
                    "label": label,
                    "object id": inp_oid,
                }
            )


async def feedback_queue_consumer(websocket):
    while True:
        next_req = await feedback_queue.get()

        serialized = json.dumps(next_req)
        await websocket.send(serialized)


async def main():
    async with websockets.connect("ws://localhost:8765/") as websocket:
        asyncio.ensure_future(infer_request_producer(websocket))
        asyncio.sleep(SLEEP_TIME)
        
        asyncio.ensure_future(feedback_queue_consumer(websocket))
        asyncio.ensure_future(infer_response_consumer(websocket))
        
        while True:
            await asyncio.sleep(SLEEP_TIME)


r.flushall()

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
