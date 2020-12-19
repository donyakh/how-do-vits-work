import io
import time
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms


@torch.no_grad()
def test(model, n_ff, dataset, num_classes,
         cutoffs=(0.0, 0.9), bins=np.linspace(0.0, 1.0, 11), verbose=False, period=10, gpu=True):
    model.eval()
    predict_times = []
    cm_shape = [num_classes, num_classes]
    cms = [[np.zeros(cm_shape), np.zeros(cm_shape)] for _ in range(len(cutoffs))]
    nll_value, brier_value, topk_value = -1.0, -1.0, -1.0
    n_acc, nll_acc, brier_acc, topk_acc = 0.0, 0.0, 0.0, 0.0
    ious, accs, uncs, freqs, eces = [], [], [], [], []

    cms_bin = [np.zeros(cm_shape) for _ in range(len(bins) - 1)]
    conf_acc_bin = [0.0 for _ in range(len(bins) - 1)]
    count_bin, acc_bin, conf_bin, metrics_str = [], [], [], []

    for step, (xs, ys) in enumerate(dataset):
        if gpu:
            xs = xs.cuda()
            ys = ys.cuda()

        # A. Predict results
        batch_time = time.time()
        ys_pred = torch.stack([F.softmax(model(xs), dim=1) for _ in range(n_ff)])
        ys_pred = torch.mean(ys_pred, dim=0)
        predict_times.append(time.time() - batch_time)

        if gpu:
            ys = ys.cpu()
            ys_pred = ys_pred.cpu()

        # B. Measure Confusion Matrices
        n_acc = n_acc + xs.size()[0]
        nll_acc = nll_acc + F.nll_loss(torch.log(ys_pred), ys, reduction="sum").item()
        topk_acc = topk_acc + np.sum(topk(ys.numpy(), ys_pred.numpy()))
        brier_acc = brier_acc + np.sum(brier(ys.numpy(), ys_pred.numpy()))

        for cutoff, cm_group in zip(cutoffs, cms):
            cm_certain = cm(ys.numpy(), ys_pred.numpy(), filter_min=cutoff)
            cm_uncertain = cm(ys.numpy(), ys_pred.numpy(), filter_max=cutoff)
            cm_group[0] = cm_group[0] + cm_certain
            cm_group[1] = cm_group[1] + cm_uncertain
        for i, (start, end) in enumerate(zip(bins, bins[1:])):
            cms_bin[i] = cms_bin[i] + cm(ys.numpy(), ys_pred.numpy(), filter_min=start, filter_max=end)
            confidence = np.amax(ys_pred.numpy(), axis=1)
            condition = np.logical_and(confidence >= start, confidence < end)
            conf_acc_bin[i] = conf_acc_bin[i] + np.sum(confidence[condition])

        nll_value = nll_acc / n_acc
        topk_value = topk_acc / n_acc
        brier_value = brier_acc / n_acc
        accs = [gacc(cm_certain) for cm_certain, cm_uncertain in cms]
        ious = [miou(cm_certain) for cm_certain, cm_uncertain in cms]
        uncs = [unconfidence(cm_certain, cm_uncertain) for cm_certain, cm_uncertain in cms]
        freqs = [frequency(cm_certain, cm_uncertain) for cm_certain, cm_uncertain in cms]
        count_bin = [np.sum(cm_bin) for cm_bin in cms_bin]
        acc_bin = [gacc(cm_bin) for cm_bin in cms_bin]
        conf_bin = [conf_acc / (count + 1e-7) for count, conf_acc in zip(count_bin, conf_acc_bin)]
        eces = ece(count_bin, acc_bin, conf_bin)

        metrics_str = [
            "Time: %.3f ± %.3f ms" % (np.mean(predict_times) * 1e3, np.std(predict_times) * 1e3),
            "NLL: %.4f" % nll_value,
            "Cutoffs: " + ", ".join(["%.1f %%" % (cutoff * 100) for cutoff in cutoffs]),
            "Accs: " + ", ".join(["%.3f %%" % (acc * 100) for acc in accs]),
            "Uncs: " + ", ".join(["%.3f %%" % (unc * 100) for unc in uncs]),
            "IoUs: " + ", ".join(["%.3f %%" % (iou * 100) for iou in ious]),
            "Freqs: " + ", ".join(["%.3f %%" % (freq * 100) for freq in freqs]),
            "Top-5: " + "%.3f %%" % (topk_value * 100),
            "Brier: " + "%.3f" % (brier_value),
            "ECE: " + "%.3f %%" % (eces * 100),
        ]
        if verbose and int(step + 1) % period is 0:
            print("%d Steps, %s" % (int(step + 1), ", ".join(metrics_str)))

    print(", ".join(metrics_str))

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    confidence_histogram(axes[0], count_bin)
    reliability_diagram(axes[1], acc_bin)
    fig.tight_layout()
    calibration_image = plot_to_image(fig)
    if not verbose:
        plt.close(fig)

    return nll_value, cms, accs, uncs, ious, freqs, \
           topk_value, brier_value, \
           count_bin, acc_bin, conf_bin, eces, calibration_image


def brier(ys, ys_pred):
    ys_onehot = np.eye(ys_pred.shape[1])[ys]
    return (np.square(ys_onehot - ys_pred)).sum(axis=1)


def topk(ys, ys_pred, k=5):
    ys_pred = ys_pred.argsort(axis=1)[:, -k:][:, ::-1]
    correct = np.logical_or.reduce(ys_pred == ys.reshape(-1, 1), axis=1)
    return correct


def cm(ys, ys_pred, filter_min=0.0, filter_max=1.0):
    """
    Confusion matrix.

    :param ys: numpy array [batch_size,]
    :param ys_pred: onehot numpy array [batch_size, num_classes]
    :param filter_min: lower bound of confidence
    :param filter_max: upper bound of confidence
    :return: cm for filtered predictions (shape: [num_classes, num_classes])
    """
    num_classes = ys_pred.shape[1]
    confidence = np.amax(ys_pred, axis=1)

    ys_pred = np.argmax(ys_pred, axis=1)
    condition = np.logical_and(confidence > filter_min, confidence <= filter_max)

    k = (ys >= 0) & (ys < num_classes) & condition
    cm = np.bincount(num_classes * ys[k] + ys_pred[k], minlength=num_classes ** 2)
    cm = np.reshape(cm, [num_classes, num_classes])

    return cm


def miou(cm):
    """
    Mean IoU
    """
    weights = np.sum(cm, axis=1)
    weights = [1 if weight > 0 else 0 for weight in weights]
    if np.sum(weights) > 0:
        _miou = np.average(ious(cm), weights=weights)
    else:
        _miou = 0.0
    return _miou


def ious(cm):
    """
    Intersection over unit w.r.t. classes.
    """
    num = np.diag(cm)
    den = np.sum(cm, axis=1) + np.sum(cm, axis=0) - np.diag(cm)
    return np.divide(num, den, out=np.zeros_like(num, dtype=float), where=(den != 0))


def gacc(cm):
    """
    Global accuracy p(accurate). For cm_certain, p(accurate|confident).
    """
    num = np.diag(cm).sum()
    den = np.sum(cm)
    return np.divide(num, den, out=np.zeros_like(num, dtype=float), where=(den != 0))


def caccs(cm):
    """
    Accuracies w.r.t. classes.
    """
    accs = []
    for ii in range(np.shape(cm)[0]):
        if float(np.sum(cm, axis=1)[ii]) == 0:
            acc = 0.0
        else:
            acc = np.diag(cm)[ii] / float(np.sum(cm, axis=1)[ii])
        accs.append(acc)
    return accs


def unconfidence(cm_certain, cm_uncertain):
    """
    p(unconfident|inaccurate)
    """
    inaccurate_certain = np.sum(cm_certain) - np.diag(cm_certain).sum()
    inaccurate_uncertain = np.sum(cm_uncertain) - np.diag(cm_uncertain).sum()

    return inaccurate_uncertain / (inaccurate_certain + inaccurate_uncertain)


def frequency(cm_certain, cm_uncertain):
    return np.sum(cm_certain) / (np.sum(cm_certain) + np.sum(cm_uncertain))


def ece(count_bin, acc_bin, conf_bin):
    count_bin = np.array(count_bin)
    acc_bin = np.array(acc_bin)
    conf_bin = np.array(conf_bin)
    freq = np.nan_to_num(count_bin / sum(count_bin))
    ece_result = sum(np.absolute(acc_bin - conf_bin) * freq)
    return ece_result


def confidence_histogram(ax, count_bin):
    color, alpha = "tab:green", 0.8
    centers = np.linspace(0.05, 0.95, 10)
    count_bin = np.array(count_bin)
    freq = count_bin / sum(count_bin)

    ax.bar(centers * 100, freq * 100, width=10, color=color, edgecolor="black", alpha=alpha)
    ax.set_xlim(0, 100.0)
    ax.set_ylim(0, 100.0)
    ax.set_xlabel("Confidence (%)")
    ax.set_ylabel("Frequency (%)")


def reliability_diagram(ax, accs_bins, colors="tab:red", mode=0):
    alpha, guideline_style = 0.8, (0, (1, 1))
    guides_x, guides_y = np.linspace(0.0, 1.0, 11), np.linspace(0.0, 1.0, 11)
    centers = np.linspace(0.05, 0.95, 10)
    accs_bins = np.array(accs_bins)
    accs_bins = np.expand_dims(accs_bins, axis=0) if len(accs_bins.shape) < 2 else accs_bins
    colors = [colors] if type(colors) is not list else colors
    colors = colors + [None] * (len(accs_bins) - len(colors))

    ax.plot(guides_x * 100, guides_y * 100, linestyle=guideline_style, color="black")
    for accs_bin, color in zip(accs_bins, colors):
        if mode is 0:
            ax.bar(centers * 100, accs_bin * 100, width=10, color=color, edgecolor="black", alpha=alpha)
        elif mode is 1:
            ax.plot(centers * 100, accs_bin * 100, color=color, marker="o", alpha=alpha)
        else:
            raise ValueError("Invalid mode %d." % mode)

    ax.set_xlim(0, 100.0)
    ax.set_ylim(0, 100.0)
    ax.set_xlabel("Confidence (%)")
    ax.set_ylabel("Accuracy (%)")


def plot_to_image(figure):
    """
    Converts the matplotlib plot specified by "figure" to a PNG image and
    returns it. The supplied figure is closed and inaccessible after this call.
    """
    # Save the plot to a PNG in memory
    buf = io.BytesIO()
    figure.savefig(buf, format="png")
    buf.seek(0)

    # Convert PNG buffer to TF image
    trans = transforms.ToTensor()
    image = buf.getvalue()
    image = Image.open(io.BytesIO(image))
    image = trans(image)

    return image


