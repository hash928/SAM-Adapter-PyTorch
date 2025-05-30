import argparse
import os
import yaml
from tqdm import tqdm
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR
import datasets
import models
import utils
from statistics import mean
import torch

def make_data_loader(spec, tag=''):
    if spec is None:
        return None

    dataset = datasets.make(spec['dataset'])
    dataset = datasets.make(spec['wrapper'], args={'dataset': dataset})
    print('{} dataset: size={}'.format(tag, len(dataset)))
    for k, v in dataset[0].items():
        print('  {}: shape={}'.format(k, tuple(v.shape)))

    loader = DataLoader(dataset, batch_size=spec['batch_size'],
        shuffle=True, num_workers=4, pin_memory=True)
    return loader

def make_data_loaders():
    train_loader = make_data_loader(config.get('train_dataset'), tag='train')
    val_loader = make_data_loader(config.get('val_dataset'), tag='val')
    return train_loader, val_loader

def eval_psnr(loader, model, eval_type=None):
    model.eval()

    if eval_type == 'f1':
        metric_fn = utils.calc_f1
        metric1, metric2, metric3, metric4 = 'f1', 'auc', 'none', 'none'
    elif eval_type == 'fmeasure':
        metric_fn = utils.calc_fmeasure
        metric1, metric2, metric3, metric4 = 'f_mea', 'mae', 'none', 'none'
    elif eval_type == 'ber':
        metric_fn = utils.calc_ber
        metric1, metric2, metric3, metric4 = 'shadow', 'non_shadow', 'ber', 'none'
    elif eval_type == 'cod':
        metric_fn = utils.calc_cod
        metric1, metric2, metric3, metric4 = 'sm', 'em', 'wfm', 'mae'

    pbar = tqdm(total=len(loader), leave=False, desc='val')

    pred_list = []
    gt_list = []
    for batch in loader:
        for k, v in batch.items():
            batch[k] = v.cuda()

        inp = batch['inp']
        pred = torch.sigmoid(model.infer(inp))
        pred_list.append(pred)
        gt_list.append(batch['gt'])
        pbar.update(1)

    pbar.close()

    pred_list = torch.cat(pred_list, 0)
    gt_list = torch.cat(gt_list, 0)
    result1, result2, result3, result4 = metric_fn(pred_list, gt_list)

    return result1, result2, result3, result4, metric1, metric2, metric3, metric4

def prepare_training():
    if config.get('resume') is not None:
        model = models.make(config['model']).cuda()
        optimizer = utils.make_optimizer(
            model.parameters(), config['optimizer'])
        epoch_start = config.get('resume') + 1
    else:
        model = models.make(config['model']).cuda()
        optimizer = utils.make_optimizer(
            model.parameters(), config['optimizer'])
        epoch_start = 1
    max_epoch = config.get('epoch_max')
    lr_scheduler = CosineAnnealingLR(optimizer, max_epoch, eta_min=config.get('lr_min'))
    print('model: #params={}'.format(utils.compute_num_params(model, text=True)))
    return model, optimizer, epoch_start, lr_scheduler

def train(train_loader, model):
    model.train()
    pbar = tqdm(total=len(train_loader), leave=False, desc='train')
    loss_list = []
    
    for batch in train_loader:
        for k, v in batch.items():
            batch[k] = v.cuda()
        inp = batch['inp']
        gt = batch['gt']
        model.set_input(inp, gt)
        model.optimize_parameters()
        loss_list.append(model.loss_G.item())
        pbar.update(1)

    pbar.close()
    return mean(loss_list)

def save_model(config, model, save_path, name):
    """保存模型和优化器状态"""
    model_spec = config['model']
    model_spec['sd'] = model.state_dict()
    optimizer_spec = config['optimizer']
    optimizer_spec['sd'] = model.optimizer.state_dict()
    
    save_file = os.path.join(save_path, f'model_{name}.pth')
    torch.save({
        'model_spec': model_spec,
        'optimizer_spec': optimizer_spec,
    }, save_file)

def main(config_, save_path):
    global config, log, writer, log_info
    config = config_
    log, writer = utils.set_save_path(save_path, remove=False)
    with open(os.path.join(save_path, 'config.yaml'), 'w') as f:
        yaml.dump(config, f, sort_keys=False)

    train_loader, val_loader = make_data_loaders()
    if config.get('data_norm') is None:
        config['data_norm'] = {
            'inp': {'sub': [0], 'div': [1]},
            'gt': {'sub': [0], 'div': [1]}
        }

    model, optimizer, epoch_start, lr_scheduler = prepare_training()
    model.optimizer = optimizer
    lr_scheduler = CosineAnnealingLR(model.optimizer, config['epoch_max'], eta_min=config.get('lr_min'))

    model = model.cuda()
    
    # 如果配置中有预训练模型路径，则加载它
    if 'sam_checkpoint' in config:
        print('Loading pretrained model from:', config['sam_checkpoint'])
        sam_checkpoint = torch.load(config['sam_checkpoint'], weights_only=True)
        model.load_state_dict(sam_checkpoint, strict=False)
        print('Pretrained model loaded successfully')
    else:
        print('Training from scratch without pretrained model')
    
    # 设置所有参数可训练
    for name, para in model.named_parameters():
        para.requires_grad_(True)
    
    model_total_params = sum(p.numel() for p in model.parameters())
    model_grad_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('model_grad_params:' + str(model_grad_params), '\nmodel_total_params:' + str(model_total_params))

    epoch_max = config['epoch_max']
    epoch_val = config.get('epoch_val')
    max_val_v = -1e18 if config['eval_type'] != 'ber' else 1e8
    timer = utils.Timer()
    
    for epoch in range(epoch_start, epoch_max + 1):
        t_epoch_start = timer.t()
        train_loss_G = train(train_loader, model)
        lr_scheduler.step()

        log_info = ['epoch {}/{}'.format(epoch, epoch_max)]
        writer.add_scalar('lr', optimizer.param_groups[0]['lr'], epoch)
        log_info.append('train G: loss={:.4f}'.format(train_loss_G))
        writer.add_scalars('loss', {'train G': train_loss_G}, epoch)

        save_model(config, model, save_path, 'last')

        if (epoch_val is not None) and (epoch % epoch_val == 0):
            result1, result2, result3, result4, metric1, metric2, metric3, metric4 = eval_psnr(val_loader, model,
                eval_type=config.get('eval_type'))

            log_info.append('val: {}={:.4f}'.format(metric1, result1))
            writer.add_scalars(metric1, {'val': result1}, epoch)
            log_info.append('val: {}={:.4f}'.format(metric2, result2))
            writer.add_scalars(metric2, {'val': result2}, epoch)
            log_info.append('val: {}={:.4f}'.format(metric3, result3))
            writer.add_scalars(metric3, {'val': result3}, epoch)
            log_info.append('val: {}={:.4f}'.format(metric4, result4))
            writer.add_scalars(metric4, {'val': result4}, epoch)

            if config['eval_type'] != 'ber':
                if result1 > max_val_v:
                    max_val_v = result1
                    save_model(config, model, save_path, 'best')
            else:
                if result3 < max_val_v:
                    max_val_v = result3
                    save_model(config, model, save_path, 'best')

            t = timer.t()
            prog = (epoch - epoch_start + 1) / (epoch_max - epoch_start + 1)
            t_epoch = utils.time_text(t - t_epoch_start)
            t_elapsed, t_all = utils.time_text(t), utils.time_text(t / prog)
            log_info.append('{} {}/{}'.format(t_epoch, t_elapsed, t_all))

            log(', '.join(log_info))
            writer.flush()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default="configs/demo.yaml")
    parser.add_argument('--name', default=None)
    parser.add_argument('--tag', default=None)
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
        print('config loaded.')

    save_name = args.name
    if save_name is None:
        save_name = '_' + args.config.split('/')[-1][:-len('.yaml')]
    if args.tag is not None:
        save_name += '_' + args.tag
    save_path = os.path.join('./save', save_name)

    main(config, save_path) 