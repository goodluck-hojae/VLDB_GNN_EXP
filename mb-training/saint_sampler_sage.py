import os
import argparse
import time

import dgl
import dgl.nn as dglnn

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchmetrics.functional as MF

import torch.distributed as dist
import torch.multiprocessing as mp

from dgl.multiprocessing import shared_tensor
from ogb.nodeproppred import DglNodePropPredDataset
from torch.nn.parallel import DistributedDataParallel
import csv
import utils 
import json 
from models import *

# Define the header
header = ['dataset', 'model', 'batch_size', 'budget', 'num_partition', 
'shuffle', 'use_ddp', 'gpu_model', 'n_gpu', 'n_layers',
'n_hidden', 'test_accuracy', 'epoch_time', 'n_50_accuracy', 'n_50_tta',
 'n_50_epoch', 'n_100_accuracy', 'n_100_tta', 'n_100_epoch']


def write_to_csv(data, file_name):
    file_exists = os.path.isfile(file_name)
    
    with open(file_name, mode='a', newline='') as file:
        writer = csv.writer(file)
        
        if not file_exists:
            writer.writerow(header)
        writer.writerows(data)




def train(
    proc_id, nprocs, device, g, num_classes, train_idx, val_idx, model, use_uva, params
):
    ds, batch_size, num_partitions, mode, budget, shuffle, use_ddp, n_gpu, n_layers, n_hidden = params 
    num_iters = num_partitions
    print(mode, budget)
    print('device', device)
    sampler = dgl.dataloading.SAINTSampler(mode=mode, budget=budget, output_device='cuda:'+str(device))
    
    dataloader = dgl.dataloading.DataLoader(
        g,
        torch.arange(num_iters),
        sampler,
        device=device,
        batch_size=batch_size,
        shuffle=shuffle,
        drop_last=False,
        num_workers=0,
        use_uva=False,
        use_ddp=use_ddp,
    )
    
    opt = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=5e-4)
    durations = []
    end=0
    best_test_acc = -1
    tta = 0
    n_epochs = 0
    best_acc_count = 0
    best_acc_count_thres = (50, 100)
    epoch_time = 0
    n_50_epoch = -1
    n_100_epoch = -1
    n_50_tta = -1
    n_100_tta = -1
    n_50_accuracy = -1
    n_100_accuracy = -1

    stop_training = torch.tensor(0, dtype=torch.int, device=device)  # Shared stop flag, initially set to 0
    epoch_data = []
    for epoch in range(10000):
        t0 = time.time()
        model.train()
        total_loss = 0
        epoch_start = time.time()
        for it, sg in enumerate(dataloader):
            # print(end-start)
            sg =sg.to(device)
            x = sg.ndata["feat"]
            y = sg.ndata["label"]
            m = sg.ndata["train_mask"].bool()
            y_hat = model(sg, x)
            loss = F.cross_entropy(y_hat[m], y[m])
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += loss
        tta += time.time() - epoch_start
            
        val_acc, test_acc = utils.evaluate(model, g, num_classes, dataloader)

        # Move to GPU before reducing
        val_acc, test_acc = val_acc.to(device), test_acc.to(device)
 
        train_acc = utils.train_evaluate(model, g, num_classes, dataloader)
        train_acc = train_acc.to(device)
        # Reduce across all GPUs
        dist.reduce(train_acc, dst=0, op=dist.ReduceOp.SUM)
        dist.reduce(val_acc, dst=0, op=dist.ReduceOp.SUM)
        dist.reduce(test_acc, dst=0, op=dist.ReduceOp.SUM)

        dist.broadcast(train_acc, 0)
        dist.broadcast(val_acc, 0)
        dist.broadcast(test_acc, 0)

        # Only process 0 should print the results
        if proc_id == 0:
            train_acc /= nprocs  # Average across GPUs
            val_acc /= nprocs  # Average across GPUs
            test_acc /= nprocs  # Average across GPUs
            if epoch % 1 == 0:
                print("Epoch {:05d} | Loss {:.4f} | Train Accuracy {:.4f} | Val Accuracy {:.4f} | Test Accuracy {:.4f}".format(
                    epoch, total_loss / (it + 1), train_acc.item(), val_acc.item(), test_acc.item()))
                tt = time.time() - t0
                
                print("Run time for epoch# %d: %.2fs" % (epoch, tt))
                epoch_data.append({
                    "epoch": epoch,
                    "train_acc": train_acc.item(),
                    "val_acc": val_acc.item(),
                    "test_acc": test_acc.item(),
                    "time": tt
                })
            
            if best_test_acc < float(test_acc.item()):
                best_test_acc = float(test_acc.item())
                best_acc_count = 0
            best_acc_count += 1
            n_epochs += 1
            if best_acc_count > best_acc_count_thres[0] and n_50_tta == -1:
                n_50_tta = tta
                n_50_epoch = n_epochs
                n_50_accuracy = best_test_acc
            if best_acc_count > best_acc_count_thres[1]:
                epoch_time = tta / n_epochs
                n_100_tta = tta
                n_100_epoch = n_epochs
                n_100_accuracy = best_test_acc
                stop_training.fill_(1)  # Set the stop flag to 1 when it's time to stop
            durations.append(tt)

        # Broadcast the stop flag to all other processes
        dist.broadcast(stop_training, src=0)
                
        if stop_training.item() == 1:
            break  # All processes will break

    if proc_id == 0:
        print(f'total time took : {tta}')
        gpu_model = torch.cuda.get_device_name(0)
        model_name = model.module.__class__.__name__
        data = ds, model_name, batch_size, budget, num_partitions, shuffle, use_ddp, gpu_model, n_gpu, n_layers, n_hidden, best_test_acc, epoch_time, n_50_accuracy, n_50_tta, n_50_epoch, n_100_accuracy, n_100_tta, n_100_epoch
        print(data)
        write_to_csv([data], 'saint_sampler_v12.csv')

 

def run(proc_id, nprocs, devices, g, data, mode, params):
    try:
        ds, batch_size, num_partition, mode, budget, shuffle, use_ddp, n_gpu, n_layers, n_hidden = params 

        device = devices[proc_id]
        torch.cuda.set_device(device)
        dist.init_process_group(
            backend="nccl",
            init_method="tcp://127.0.0.1:12347",
            world_size=nprocs,
            rank=proc_id,
        )
        num_classes, train_idx, val_idx, test_idx = data 
        train_idx = train_idx.to(device)
        val_idx = val_idx.to(device)
        g = g.to(device if mode == "puregpu" else "cpu")

        in_size = g.ndata["feat"].shape[1] 
        n_classes = num_classes
        
        model = GraphSAGE(in_size, n_hidden, n_classes, n_layers, activation=F.relu, dropout=0.3).to(device)
        model = DistributedDataParallel(
            model, device_ids=[device], output_device=device
        )
        # training + testing
        use_uva = mode == "mixed"
        train(
            proc_id,
            nprocs,
            device,
            g,
            num_classes,
            train_idx,
            val_idx,
            model,
            use_uva,
            params
        )

    except Exception as e:
        print(f"Exception in process {proc_id}: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # Ensure cleanup
        dist.destroy_process_group()  # Ensures the process group is destroyed, freeing up resources
        torch.cuda.empty_cache()  # Free up unused memory on the GPU


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        default="puregpu",
        choices=["mixed", "puregpu"],
        help="Training mode. 'mixed' for CPU-GPU mixed training, "
        "'puregpu' for pure-GPU training.",
    )
    parser.add_argument(
        "--gpu",
        type=str,
        default="0,1",
        help="GPU(s) in use. Can be a list of gpu ids for multi-gpu training,"
        " e.g., 0,1,2,3.",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="pubmed",
        help="Selection for Dataset "
    )
    args = parser.parse_args()
    devices = list(map(int, args.gpu.split(",")))
    nprocs = len(devices)
    assert (
        torch.cuda.is_available()
    ), f"Must have GPUs to enable multi-gpu training."

    # load and preprocess dataset
    print("Loading data")
    if args.dataset == 'pubmed':
        dataset = utils.load_pubmed()
        graph = dataset[0]
        data = (
            dataset.num_classes,
            dataset.train_idx,
            dataset.val_idx,
            dataset.test_idx,
        )
        model_params = (3, 256)
        batch_size = 1024
        budget = 1000
        partition = 1000
        mode = 'node'

    elif args.dataset == 'ogbn-arxiv':
        dataset = utils.load_data('ogbn-arxiv')
        graph = dataset[0]
        data = (
            dataset.num_classes,
            dataset.train_idx,
            dataset.val_idx,
            dataset.test_idx,
        )
        model_params = (2, 512)
        batch_size = 1024
        budget = (10000, 10)
        partition = 5000
        mode = 'walk'

    elif args.dataset == 'reddit':
        dataset = utils.load_reddit()
        graph = dataset[0]
        data = (
            dataset.num_classes,
            dataset.train_idx,
            dataset.val_idx,
            dataset.test_idx,
        )
        model_params = (4, 1024)
        batch_size = 1024
        budget = 10000
        partition = 1000
        mode = 'node'

    elif args.dataset == 'ogbn-products':
        dataset = utils.load_data('ogbn-products')
        graph = dataset[0]
        data = (
            dataset.num_classes,
            dataset.train_idx,
            dataset.val_idx,
            dataset.test_idx,
        )
        model_params = (3, 256)
        batch_size = 1024
        budget = (5000, 5)
        partition = 3000
        mode = 'walk'

    elif args.dataset == 'ogbn-papers100M':
        dataset = utils.load_data('ogbn-papers100M')
        graph = dataset[0]
        train_ids = torch.nonzero(dataset.ndata['train_mask'], as_tuple=False).reshape(-1)
        valid_ids = torch.nonzero(dataset.ndata['val_mask'], as_tuple=False).reshape(-1)
        test_ids = torch.nonzero(dataset.ndata['test_mask'], as_tuple=False).reshape(-1)
        data = (
            dataset.num_classes, train_ids, valid_ids, test_ids
        )
        model_params = (2, 128)
        batch_size = 128
        budget = (5000, 5)
        partition = 10000
        mode = 'walk'

    # Store configurations in a dictionary for easy access
    dataset = {
        'graph': graph,
        'data': data,
        'model_params': model_params,
        'batch_size': batch_size,
        'mode': mode,
        'budget': budget,
        'partition': partition
    }

    os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
 
    shuffle = [True]
    use_ddp = [True]
    mode = ['walk'] 
    n_gpu = ['0', '0,1', '0,1,2', '0,1,2,3']
    n_gpu = ['0,1,2,3']
    n_gpu.reverse()


    i = 0
    l = dataset['model_params'][0]
    h = dataset['model_params'][1]
    b = dataset['batch_size']
    p = dataset['partition']
    bu = dataset['budget']
    m = dataset['mode']
    g = dataset['graph']  # already prepares ndata['label'/'train_mask'/'val_mask'/'test_mask']
    # avoid creating certain graph formats in each sub-process to save momory
    if args.dataset in ["ogbn-arxiv", 'ogbn-papers100M', 'orkut'] :
        g.edata.clear()
        g = dgl.to_bidirected(g, copy_ndata=True)
        g = dgl.remove_self_loop(g)
        g = dgl.add_self_loop(g)
    else:
        g.edata.clear()
        g = dgl.remove_self_loop(g)
        g = dgl.add_self_loop(g)
    g.create_formats_()
    data = dataset['data']

    for s in shuffle:
        for d in use_ddp:
            devices = list(map(int, args.gpu.split(",")))
            nprocs = len(devices) 
            print(devices, nprocs)
            print(f"{i} th training in {args.mode} mode using {nprocs} GPU(s)")
            os.environ["OMP_NUM_THREADS"] = str(mp.cpu_count() // 2 // nprocs)
            print('Running parameters : ', args.dataset, b, p, m, bu, s, d, args.gpu, l, h)
            mp.spawn(run, args=(nprocs, devices, g, data, args.mode, (args.dataset, b, p, m, bu, s, d, args.gpu, l, h)), nprocs=nprocs, join=True)
            i += 1

