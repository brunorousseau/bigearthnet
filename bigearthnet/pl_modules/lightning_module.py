import logging
import typing

import matplotlib.pyplot as plt
import pytorch_lightning as pl
import torch
from hydra.utils import instantiate
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    classification_report,
    multilabel_confusion_matrix,
    precision_recall_fscore_support,
)
from torch import optim

log = logging.getLogger(__name__)


class LitModel(pl.LightningModule):
    """Base class for Pytorch Lightning model."""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.model = instantiate(cfg.model)
        self.loss_fn = torch.nn.BCEWithLogitsLoss()

    def on_train_start(self):
        mode = self.cfg.monitor.mode
        name = self.cfg.monitor.name
        assert mode in ["min", "max"]
        assert name in ["loss", "precision", "recall", "f1_score"]
        # initial metrics before training
        init_metrics = {
            "best_metrics/loss": 99999,
            "best_metrics/precision": 0,
            "best_metrics/recall": 0,
            "best_metrics/f1_score": 0,
        }

        self.logger.log_hyperparams(self.cfg, metrics=init_metrics)

        self.best_metric = init_metrics[f"best_metrics/{name}"]

        # get the classes
        self.class_names: typing.List = (
            self.trainer.train_dataloader.dataset.datasets.class_names
        )

    def configure_optimizers(self):
        name = self.cfg.optimizer.name
        lr = self.cfg.optimizer.lr
        if name == "adam":
            optimizer = optim.Adam(
                self.model.parameters(),
                lr=lr,
            )
        elif name == "sgd":
            optimizer = optim.SGD(self.model.parameters(), lr=lr)
        else:
            raise ValueError(f"optimizer {name} not supported")
        return optimizer

    def _generic_step(self, batch, batch_idx):
        """Runs the prediction + evaluation step for training/validation/testing."""
        inputs = batch["data"]
        targets = batch["labels"]
        logits = self.model(inputs)
        loss = self.loss_fn(logits, targets.float())
        return {"loss": loss, "targets": targets, "logits": logits}

    def _generic_epoch_end(self, step_outputs):

        all_targets = []
        all_preds = []
        all_loss = []
        for outputs in step_outputs:
            logits = outputs["logits"]
            targets = outputs["targets"]
            preds = torch.sigmoid(logits) > 0.5
            all_targets.extend(targets.cpu().numpy())
            all_preds.extend(preds.type(targets.dtype).cpu().numpy())

            loss = outputs["loss"]
            all_loss.append(loss.cpu().numpy())

        prec, rec, f1, s = precision_recall_fscore_support(
            y_true=all_targets, y_pred=all_preds, average="micro"
        )
        avg_loss = sum(all_loss) / len(all_loss)
        conf_mats = multilabel_confusion_matrix(y_true=all_targets, y_pred=all_preds)
        report = classification_report(
            y_true=all_targets, y_pred=all_preds, target_names=self.class_names
        )

        metrics = {
            "precision": prec,
            "recall": rec,
            "f1_score": f1,
            "conf_mats": conf_mats,
            "report": report,
            "loss": avg_loss,
        }
        return metrics

    def training_step(self, batch, batch_idx):
        """Runs a prediction step for training, returning the loss."""
        outputs = self._generic_step(batch, batch_idx)
        self.log(
            "loss/train",
            outputs["loss"],
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )
        return outputs

    def training_epoch_end(self, training_step_outputs):
        metrics = self._generic_epoch_end(training_step_outputs)
        self.log_metrics(metrics, split="train")

    def validation_step(self, batch, batch_idx):
        """Runs a prediction step for validation, logging the loss."""
        outputs = self._generic_step(batch, batch_idx)
        self.log(
            "loss/val",
            outputs["loss"],
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )
        return outputs

    def validation_epoch_end(self, validation_step_outputs):
        if not self.trainer.sanity_checking:
            metrics = self._generic_epoch_end(validation_step_outputs)
            self.log_metrics(metrics, split="val")
            self.update_best_metric(metrics)

    def test_step(self, batch, batch_idx):
        """Runs a prediction step for testing, logging the loss."""
        outputs = self._generic_step(batch, batch_idx)
        return outputs

    def test_epoch_end(self, test_step_outputs):
        metrics = self._generic_epoch_end(test_step_outputs)
        self.log_metrics(metrics, split="test")

    def log_metrics(self, metrics: typing.Dict, split: str):
        # log to tensorboard
        self.log(f"precision/{split}", metrics["precision"], on_epoch=True)
        self.log(f"recall/{split}", metrics["recall"], on_epoch=True)
        self.log(f"f1_score/{split}", metrics["f1_score"], on_epoch=True)

        # add to logs
        log.info(f"{split} epoch: {self.current_epoch}")
        log.info(f"{split} classification report:\n{metrics['report']}")

        # Here we log the confusion matrices in the logs as well as images in tensorboard
        conf_mats = metrics["conf_mats"]
        conf_mat_log = f"{split} Confusion matrices:\n:"
        fig, axs = plt.subplots(9, 5, figsize=(12, 15))
        [ax.set_axis_off() for ax in axs.ravel()]
        for cm, label, ax in zip(conf_mats, self.class_names, axs.ravel()):
            # add to log
            conf_mat_log += f"\n{label}\n{cm}\n"

            # add to figure
            disp = ConfusionMatrixDisplay(
                confusion_matrix=cm,
            )
            disp.plot(ax=ax, colorbar=False)
            ax.title.set_text(label[0:20])  # text cutoff

        self.logger.experiment.add_figure(
            f"confusion matrix/{split}", fig, self.global_step
        )
        log.info(conf_mat_log)
        plt.close(fig)

    def update_best_metric(self, metrics):
        """Update the best scoring metric for parallel coordinate plots."""
        mode = self.cfg.monitor.mode
        name = self.cfg.monitor.name
        update = False
        if mode == "min" and metrics[name] < self.best_metric:
            update = True
        if mode == "max" and metrics[name] > self.best_metric:
            update = True
        if update:
            self.logger.log_hyperparams(
                self.cfg,
                metrics={
                    f"best_metrics/{k}": metrics[k]
                    for k in ["loss", "precision", "recall", "f1_score"]
                },
            )
            self.best_metric = metrics[name]
