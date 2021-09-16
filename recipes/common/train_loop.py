import torch
from hydra.utils import to_absolute_path
from tqdm import tqdm
from pathlib import Path
from vc_tts_template.train_utils import (
    get_epochs_with_optional_tqdm,
    save_checkpoint
)


def _train_step(
    model,
    optimizer,
    lr_scheduler,
    train,
    loss,
    batch,
    logger
):
    optimizer.zero_grad()

    # Run forwaard
    output = model(*batch)

    loss, loss_values = loss(batch, output)

    # Update
    if train:
        loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        if not torch.isfinite(grad_norm):
            # こんなことあるんだ.
            logger.info("grad norm is NaN. Skip updating")
        else:
            optimizer.step()
        lr_scheduler.step()

    return loss_values


def _update_running_losses_(running_losses, loss_values):
    for key, val in loss_values.items():
        try:
            running_losses[key] += val
        except KeyError:
            running_losses[key] = val


def train_loop(config, to_device, model, optimizer, lr_scheduler, loss, data_loaders,
               writers, logger, eval_model, train_step=None, epoch_step=False, last_epoch=0, last_train_iter=0):
    out_dir = Path(to_absolute_path(config.train.out_dir))
    best_loss = torch.finfo(torch.float32).max
    train_iter = last_train_iter + 1
    nepochs = config.train.nepochs

    for epoch in get_epochs_with_optional_tqdm(config.tqdm, nepochs, last_epoch=last_epoch):
        for phase in data_loaders.keys():
            train = phase.startswith("train")
            if isinstance(model, dict):
                # hifiganのように複数modelを持つ場合, dictで管理.
                for key in model.keys():
                    model[key].train() if train else model[key].eval()
            else:
                model.train() if train else model.eval()
            running_losses = {}  # epoch毎のloss. ここでresetしてるし.
            is_first = 1
            group_size = 0
            for batchs in tqdm(
                data_loaders[phase], desc=f"{phase} iter", leave=False
            ):
                for batch in batchs:
                    batch = to_device(batch, phase)
                    train_step = _train_step if train_step is None else train_step
                    loss_values = train_step(
                        model,
                        optimizer,
                        lr_scheduler,
                        train,
                        loss,
                        batch,
                        logger
                    )
                    if train:
                        for key, val in loss_values.items():
                            writers[phase].add_scalar(f"loss_bystep/{key}", val, train_iter)
                        writers[phase].add_scalar(
                            "LearningRate", lr_scheduler.get_last_lr()[0], train_iter
                        )
                        train_iter += 1
                    # lossを一気に足してためておく. 賢い.
                    _update_running_losses_(running_losses, loss_values)

                    # 最初の検証用データに対して、中間結果の可視化
                    if (
                        is_first == 1  # 最初
                        and epoch % config.train.eval_epoch_interval == 0
                    ):
                        for is_inference in [False, True]:  # 非推論モードでやるの偉い.
                            eval_model(
                                phase,
                                train_iter,
                                model,
                                writers[phase],
                                batch,
                                is_inference
                            )
                    if is_first == 1:
                        # 一番最初のではかる. 最後だと端数になってしまう恐れ.
                        group_size = len(batchs)
                    is_first = 0

            # Epoch ごとのロスを出力
            for key, val in running_losses.items():
                ave_loss = val / (len(data_loaders[phase]) * group_size)
                writers[phase].add_scalar(f"loss/{key}", ave_loss, epoch)

            ave_loss = running_losses[list(running_losses.keys())[-1]] / (len(data_loaders[phase]) * group_size)
            if not train and ave_loss < best_loss:
                best_loss = ave_loss
                save_checkpoint(logger, out_dir, model, optimizer, lr_scheduler, epoch, train_iter, True)

            if epoch_step is True:
                lr_scheduler.step()

        if epoch % config.train.checkpoint_epoch_interval == 0:
            save_checkpoint(logger, out_dir, model, optimizer, lr_scheduler, epoch, train_iter, False)

    # save at last epoch
    save_checkpoint(logger, out_dir, model, optimizer, lr_scheduler, nepochs, train_iter)
    logger.info(f"The best loss was {best_loss}")

    return model
