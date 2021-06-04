# -*- encoding:utf -*-
"""
  This script provides an K-BERT example for NER.
"""
import random
import argparse
import torch
import torch.nn as nn
from torch.nn import CrossEntropyLoss
from uer.model_builder import build_model
from uer.utils.config import load_hyperparam
from uer.utils.optimizers import  BertAdam
from uer.utils.constants import *
from uer.utils.vocab import Vocab
from uer.utils.seed import set_seed
from uer.model_saver import save_model
import numpy as np
from brain import KnowledgeGraph
from torchcrf import CRF

# # 禁用cudnn
# torch.backends.cudnn.enabled = False

import os
# set visible gpus that can be seen by os
# os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"
os.environ["CUDA_VISIBLE_DEVICES"] = "4,5,6,7"

class BertGruCrf(nn.Module):
    def __init__(self, args, bertmodel):
        """

        :param args: 命令行参数的实例对象
        :param model: a bertmodel instance
        """
        # config
        super(BertGruCrf, self).__init__()
        self.hidden_size = args.hidden_size
        self.dropout = args.dropout
        self.tag_to_ix, self.begin_ids = args.labels_map, args.begin_ids
        self.tagset_size = len(self.tag_to_ix)

        # 创建网络结构
        self.embedding = bertmodel.embedding
        self.encoder = bertmodel.encoder
        self.target = bertmodel.target  # not used here
        # self.dropout = nn.Dropout(p=self.dropout)  # dropout 暂时没用到
        # biGru结构, 输入形式 (seq, batch, feature)
        self.rnn = nn.GRU(self.hidden_size, self.hidden_size // 2, num_layers=1, bidirectional=True, batch_first=False)
        self.dropout_layer = nn.Dropout(p=self.dropout)
        # 将gru的输出映射到标签空间
        self.hidden2tag = nn.Linear(self.hidden_size, self.tagset_size)
        # crf
        self.crf = CRF(self.tagset_size, batch_first=False)

    def forward(self, src, label, mask, pos=None, vm=None, padding_mask=None, batch_sequence_max_len=None):
        """
        Args:
            src: means token_ids  [batch_size x seq_length]
            label: means ner label_ids  [batch_size x seq_length]
            mask: [batch_size x seq_length]
            vm: [batch_size x seq_length x seq_length]
            padding_mask: [batch_size x seq_length] such as [[1,1,1,1,1,0,0,0], [1,1,1,1,1,1,1,1]]  用于求rnn和crf的mask
            batch_sequence_max_len: int, refers to the true max length of sentence in the current input batch   用于实现当前batch的动态句子最大长度
        Returns:
            loss: Sequence labeling loss.
            correct: Number of labels that are predicted correctly.
            predict: Predicted label.
            label: Gold label.
        """
        if not hasattr(self, '_flattened'):
            self.rnn.flatten_parameters()
            setattr(self, '_flattened', True)
        # get batch_sequence_max_len
        if batch_sequence_max_len is None or batch_sequence_max_len <= 0:
            batch_sequence_max_len = src.shape[1]
        # reshape输入的数据-基于batch_sequence_max_len动态调整src/label/mask/pos/vm/padding_mask的长度，减少对无用padding的计算
        src = src[:, :batch_sequence_max_len]
        label = label[:, :batch_sequence_max_len]
        mask = mask[:, :batch_sequence_max_len]
        if pos is not None:
            pos = pos[:, :batch_sequence_max_len]
        if vm is not None:
            vm = vm[:, :batch_sequence_max_len, :batch_sequence_max_len]
        if padding_mask is not None:
            padding_mask = padding_mask[:, :batch_sequence_max_len]

        # embedding
        embeds = self.embedding(src, mask, pos)
        # Encoder.
        output = self.encoder(embeds, mask, vm)

        # batch_size
        bs = output.shape[0]

        output = output.transpose(0, 1)  # 转置0维和1维， 当rnn batch_first=False时需要进行转置

        # rnn.pack_padded_sequence
        if padding_mask is not None:
            true_lengths = torch.sum(padding_mask, dim=1).to(label.device)  # batch中每个句子的真实长度，放到对应gpu上
            output = nn.utils.rnn.pack_padded_sequence(output, true_lengths, batch_first=False, enforce_sorted=False)

        rnn_out, _ = self.rnn(output)

        # rnn.pad_packed_sequence
        if padding_mask is not None:
            rnn_out, lens_unpacked = nn.utils.rnn.pad_packed_sequence(rnn_out, total_length=batch_sequence_max_len)
            crf_mask = padding_mask.transpose(0, 1).contiguous().byte()  # mask for crf (batch_sequence_max_len, batch_size)
        else:
            crf_mask = None
        # dropout
        rnn_out = self.dropout_layer(rnn_out)
        # Get the emission scores from rnn. shape is (seq_length, batch_size, num_tags)
        rnn_feats = self.hidden2tag(rnn_out)

        # 转置label为(seq_length, batch_size)，适配crf的输入
        label_T = label.transpose(0, 1).contiguous()
        # Find the best path, and get the negative_log_likelihood loss according to tags
        loss, predict = self.crf(rnn_feats, label_T, mask=crf_mask), self.crf.decode(rnn_feats)
        # 取batch的avg loss
        loss = (-loss) / bs
        # list to tensor must be put into the same device
        predict = torch.LongTensor(predict).to(label.device)

        ### 计算correct
        # 将真实label转化为 1 * x 的矩阵， x表示token数量，也就是个一维的tensor
        label = label.contiguous().view(-1)
        # 同样拉直predict
        predict = predict.contiguous().view(-1)
        label_mask = (label > 0).float().to(label.device)  # label中的元素大于0则转换为1.0，即非padding字符对应的mask为1, padding对应0
        # view(-1) 表示 label_mask拉长为 1 * x 矩阵， x表示token数量
        label_mask = label_mask.contiguous().view(-1)
        predict = predict * label_mask.long()
        correct = torch.sum((predict.eq(label)).float())

        return loss, correct, predict, label

class BertLstmCrf(nn.Module):
    def __init__(self, args, bertmodel):
        """

        :param args: 命令行参数的实例对象
        :param model: a bertmodel instance
        """
        # config
        super(BertLstmCrf, self).__init__()
        self.hidden_size = args.hidden_size
        self.dropout = args.dropout
        self.tag_to_ix, self.begin_ids = args.labels_map, args.begin_ids
        self.tagset_size = len(self.tag_to_ix)

        # 创建网络结构
        self.embedding = bertmodel.embedding
        self.encoder = bertmodel.encoder
        self.target = bertmodel.target  # not used here
        # self.dropout = nn.Dropout(p=self.dropout)  # dropout 暂时没用到
        # bilstm结构, 输入形式 (seq, batch, feature)
        self.rnn = nn.LSTM(self.hidden_size, self.hidden_size // 2, num_layers=1, bidirectional=True, batch_first=False)
        self.dropout_layer = nn.Dropout(p=self.dropout)
        # 将LSTM的输出映射到标签空间
        self.hidden2tag = nn.Linear(self.hidden_size, self.tagset_size)
        # crf
        self.crf = CRF(self.tagset_size, batch_first=False)

    def forward(self, src, label, mask, pos=None, vm=None, padding_mask=None, batch_sequence_max_len=None):
        """
        Args:
            src: means token_ids  [batch_size x seq_length]
            label: means ner label_ids  [batch_size x seq_length]
            mask: [batch_size x seq_length]
            vm: [batch_size x seq_length x seq_length]
            padding_mask: [batch_size x seq_length] such as [[1,1,1,1,1,0,0,0], [1,1,1,1,1,1,1,1]]  用于求rnn和crf的mask
            batch_sequence_max_len: int, refers to the true max length of sentence in the current input batch   用于实现当前batch的动态句子最大长度
        Returns:
            loss: Sequence labeling loss.
            correct: Number of labels that are predicted correctly.
            predict: Predicted label.
            label: Gold label.
        """
        if not hasattr(self, '_flattened'):
            self.rnn.flatten_parameters()
            setattr(self, '_flattened', True)
        # get batch_sequence_max_len
        if batch_sequence_max_len is None or batch_sequence_max_len <= 0:
            batch_sequence_max_len = src.shape[1]
        # reshape输入的数据-基于batch_sequence_max_len动态调整src/label/mask/pos/vm/padding_mask的长度，减少对无用padding的计算
        src = src[:, :batch_sequence_max_len]
        label = label[:, :batch_sequence_max_len]
        mask = mask[:, :batch_sequence_max_len]
        if pos is not None:
            pos = pos[:, :batch_sequence_max_len]
        if vm is not None:
            vm = vm[:, :batch_sequence_max_len, :batch_sequence_max_len]
        if padding_mask is not None:
            padding_mask = padding_mask[:, :batch_sequence_max_len]

        # embedding
        embeds = self.embedding(src, mask, pos)
        # Encoder.
        output = self.encoder(embeds, mask, vm)

        # batch_size
        bs = output.shape[0]

        output = output.transpose(0, 1)  # 转置0维和1维， 当rnn batch_first=False时需要进行转置

        # rnn.pack_padded_sequence
        if padding_mask is not None:
            true_lengths = torch.sum(padding_mask, dim=1).to(label.device)  # batch中每个句子的真实长度，放到对应gpu上
            output = nn.utils.rnn.pack_padded_sequence(output, true_lengths, batch_first=False, enforce_sorted=False)

        lstm_out, _ = self.rnn(output)

        # rnn.pad_packed_sequence
        if padding_mask is not None:
            lstm_out, lens_unpacked = nn.utils.rnn.pad_packed_sequence(lstm_out, total_length=batch_sequence_max_len)
            crf_mask = padding_mask.transpose(0, 1).contiguous().byte()  # mask for crf (batch_sequence_max_len, batch_size)
        else:
            crf_mask = None
        lstm_out = self.dropout_layer(lstm_out)
        # Get the emission scores from the BiLSTM. shape is (seq_length, batch_size, num_tags)
        lstm_feats = self.hidden2tag(lstm_out)

        # 转置label为(seq_length, batch_size)，适配crf的输入
        label_T = label.transpose(0, 1).contiguous()
        # Find the best path, and get the negative_log_likelihood loss according to tags
        loss, predict = self.crf(lstm_feats, label_T, mask=crf_mask), self.crf.decode(lstm_feats)
        # 取batch的avg loss
        loss = (-loss) / bs
        # list to tensor must be put into the same device
        predict = torch.LongTensor(predict).to(label.device)

        ### 计算correct
        # 将真实label转化为 1 * x 的矩阵， x表示token数量，也就是个一维的tensor
        label = label.contiguous().view(-1)
        # 同样拉直predict
        predict = predict.contiguous().view(-1)
        label_mask = (label > 0).float().to(label.device)  # label中的元素大于0则转换为1.0，即非padding字符对应的mask为1, padding对应0
        # view(-1) 表示 label_mask拉长为 1 * x 矩阵， x表示token数量
        label_mask = label_mask.contiguous().view(-1)
        predict = predict * label_mask.long()
        correct = torch.sum((predict.eq(label)).float())

        return loss, correct, predict, label

class BertLstm(nn.Module):
    def __init__(self, args, bertmodel):
        """

        :param args: 命令行参数的实例对象
        :param model: a bertmodel instance
        """
        # config
        super(BertLstm, self).__init__()
        self.hidden_size = args.hidden_size
        self.dropout = args.dropout
        self.tag_to_ix, self.begin_ids = args.labels_map, args.begin_ids
        self.tagset_size = len(self.tag_to_ix)

        # 创建网络结构
        self.embedding = bertmodel.embedding
        self.encoder = bertmodel.encoder
        self.target = bertmodel.target
        # self.dropout = nn.Dropout(p=self.dropout)  # dropout 暂时没用到

        # rnn结构, 输入形式 (batch, seq, feature)
        self.rnn = nn.LSTM(self.hidden_size, self.hidden_size // 2, num_layers=1, bidirectional=True, batch_first=False)
        self.dropout_layer = nn.Dropout(p=self.dropout)
        # 将LSTM的输出映射到标签空间
        self.hidden2tag = nn.Linear(self.hidden_size, self.tagset_size)
        self.softmax = nn.LogSoftmax(dim=-1)

    # forward接收的参数仿照kbert ner demo的源码，self, src, label, mask, pos, vm
    # forward返回值也同样仿照kbert ner demo的源码，return loss, correct, predict, label
    def forward(self, src, label, mask, pos=None, vm=None, padding_mask=None, batch_sequence_max_len=None):
        if not hasattr(self, '_flattened'):
            self.rnn.flatten_parameters()
            setattr(self, '_flattened', True)
        # get batch_sequence_max_len
        if batch_sequence_max_len is None or batch_sequence_max_len <= 0:
            batch_sequence_max_len = src.shape[1]
        # reshape输入的数据-基于batch_sequence_max_len动态调整src/label/mask/pos/vm/padding_mask的长度，减少对无用padding的计算
        src = src[:, :batch_sequence_max_len]
        label = label[:, :batch_sequence_max_len]
        mask = mask[:, :batch_sequence_max_len]
        if pos is not None:
            pos = pos[:, :batch_sequence_max_len]
        if vm is not None:
            vm = vm[:, :batch_sequence_max_len, :batch_sequence_max_len]
        if padding_mask is not None:
            padding_mask = padding_mask[:, :batch_sequence_max_len]
        # embedding
        embeds = self.embedding(src, mask, pos)
        # Encoder.
        output = self.encoder(embeds, mask, vm)
        output = output.transpose(0, 1)  # 转置0维和1维， 当rnn batch_first=False时需要进行转置

        # rnn.pack_padded_sequence
        if padding_mask is not None:
            true_lengths = torch.sum(padding_mask, dim=1).to(label.device)  # batch中每个句子的真实长度，放到对应gpu上
            output = nn.utils.rnn.pack_padded_sequence(output, true_lengths, batch_first=False, enforce_sorted=False)

        rnn_out, _ = self.rnn(output)

        # rnn.pad_packed_sequence
        if padding_mask is not None:
            rnn_out, lens_unpacked = nn.utils.rnn.pad_packed_sequence(rnn_out, total_length=batch_sequence_max_len)

        rnn_out = rnn_out.transpose(0, 1)
        rnn_out = self.dropout_layer(rnn_out)
        rnn_feats = self.hidden2tag(rnn_out)
        # result
        # 通过softmax输出每个token对应各个ner label的概率
        output = self.softmax(rnn_feats)
        # view(-1, self.tagset_size)指将output转化为 batch token_nums * ner_label_nums 的矩阵
        output = output.contiguous().view(-1, self.tagset_size)

        ###### 拉直train data and label，计算loss
        # 将真实label转化为 x * 1的矩阵， x表示token数量，直观上看就是一个label作为新矩阵的一行
        label = label.contiguous().view(-1, 1)
        label_mask = (label > 0).float().to(torch.device(label.device))  # label中的元素大于0则转换为1.0，即非padding字符对应的mask为1
        # one_hot：token_nums * ner_label_nums 的 one hot 矩阵，size与output相同，值为1指示真实标签
        one_hot = torch.zeros(label_mask.size(0), self.tagset_size). \
            to(torch.device(label.device)). \
            scatter_(1, label, 1.0)
        # label smooth
        epsilon = 0.1
        # 平滑后的标签有1-epsilon的概率来自于原分布，有epsilon的概率来自于均匀分布
        label_smooth = (1 - epsilon) * one_hot + epsilon / self.tagset_size

        # * 表示同位置元素相乘
        numerator = -torch.sum(output * label_smooth, 1)
        # label_mask拉长为 1 * x 矩阵， x表示token数量
        label_mask = label_mask.contiguous().view(-1)
        # label拉长为 1 * x 矩阵， x表示token数量
        label = label.contiguous().view(-1)
        # sum  矩阵内所有元素求和，得到分子，表示
        numerator = torch.sum(label_mask * numerator)
        loss = numerator
        predict = output.argmax(dim=-1)
        correct = torch.sum(
            label_mask * (predict.eq(label)).float()
        )

        return loss, correct, predict, label

class BertGru(nn.Module):
    def __init__(self, args, bertmodel):
        """

        :param args: 命令行参数的实例对象
        :param model: a bertmodel instance
        """
        # config
        super(BertGru, self).__init__()
        self.hidden_size = args.hidden_size
        self.dropout = args.dropout
        self.tag_to_ix, self.begin_ids = args.labels_map, args.begin_ids
        self.tagset_size = len(self.tag_to_ix)

        # 创建网络结构
        self.embedding = bertmodel.embedding
        self.encoder = bertmodel.encoder
        self.target = bertmodel.target

        # rnn结构, 输入形式 (batch, seq, feature)
        self.rnn = nn.GRU(self.hidden_size, self.hidden_size // 2, num_layers=1, bidirectional=True, batch_first=False)
        self.dropout_layer = nn.Dropout(p=self.dropout)
        # 将LSTM的输出映射到标签空间
        self.hidden2tag = nn.Linear(self.hidden_size, self.tagset_size)
        self.softmax = nn.LogSoftmax(dim=-1)

    # forward接收的参数仿照kbert ner demo的源码，self, src, label, mask, pos, vm
    # forward返回值也同样仿照kbert ner demo的源码，return loss, correct, predict, label
    def forward(self, src, label, mask, pos=None, vm=None, padding_mask=None, batch_sequence_max_len=None):
        if not hasattr(self, '_flattened'):
            self.rnn.flatten_parameters()
            setattr(self, '_flattened', True)
        # get batch_sequence_max_len
        if batch_sequence_max_len is None or batch_sequence_max_len <= 0:
            batch_sequence_max_len = src.shape[1]
        # reshape输入的数据-基于batch_sequence_max_len动态调整src/label/mask/pos/vm/padding_mask的长度，减少对无用padding的计算
        src = src[:, :batch_sequence_max_len]
        label = label[:, :batch_sequence_max_len]
        mask = mask[:, :batch_sequence_max_len]
        if pos is not None:
            pos = pos[:, :batch_sequence_max_len]
        if vm is not None:
            vm = vm[:, :batch_sequence_max_len, :batch_sequence_max_len]
        if padding_mask is not None:
            padding_mask = padding_mask[:, :batch_sequence_max_len]
        # embedding
        embeds = self.embedding(src, mask, pos)
        # Encoder.
        output = self.encoder(embeds, mask, vm)
        output = output.transpose(0, 1)  # 转置0维和1维， 当rnn batch_first=False时需要进行转置

        # rnn.pack_padded_sequence
        if padding_mask is not None:
            true_lengths = torch.sum(padding_mask, dim=1).to(label.device)  # batch中每个句子的真实长度，放到对应gpu上
            output = nn.utils.rnn.pack_padded_sequence(output, true_lengths, batch_first=False, enforce_sorted=False)

        rnn_out, _ = self.rnn(output)

        # rnn.pad_packed_sequence
        if padding_mask is not None:
            rnn_out, lens_unpacked = nn.utils.rnn.pad_packed_sequence(rnn_out, total_length=batch_sequence_max_len)

        rnn_out = rnn_out.transpose(0, 1)
        rnn_out = self.dropout_layer(rnn_out)
        rnn_feats = self.hidden2tag(rnn_out)
        # result
        # 通过softmax输出每个token对应各个ner label的概率
        output = self.softmax(rnn_feats)
        # view(-1, self.tagset_size)指将output转化为 batch token_nums * ner_label_nums 的矩阵
        output = output.contiguous().view(-1, self.tagset_size)

        ###### 拉直train data and label，计算loss
        # 将真实label转化为 x * 1的矩阵， x表示token数量，直观上看就是一个label作为新矩阵的一行
        label = label.contiguous().view(-1, 1)
        label_mask = (label > 0).float().to(torch.device(label.device))  # label中的元素大于0则转换为1.0，即非padding字符对应的mask为1
        # one_hot：token_nums * ner_label_nums 的 one hot 矩阵，size与output相同，值为1指示真实标签
        one_hot = torch.zeros(label_mask.size(0), self.tagset_size). \
            to(torch.device(label.device)). \
            scatter_(1, label, 1.0)
        # label smooth
        epsilon = 0.1
        # 平滑后的标签有1-epsilon的概率来自于原分布，有epsilon的概率来自于均匀分布
        label_smooth = (1 - epsilon) * one_hot + epsilon / self.tagset_size
        # * 表示同位置元素相乘
        numerator = -torch.sum(output * label_smooth, 1)
        # label_mask拉长为 1 * x 矩阵， x表示token数量
        label_mask = label_mask.contiguous().view(-1)
        # label拉长为 1 * x 矩阵， x表示token数量
        label = label.contiguous().view(-1)
        # sum  矩阵内所有元素求和，得到分子，表示
        numerator = torch.sum(label_mask * numerator)
        loss = numerator
        predict = output.argmax(dim=-1)
        correct = torch.sum(
            label_mask * (predict.eq(label)).float()
        )

        return loss, correct, predict, label

class BertCrf(nn.Module):
    def __init__(self, args, model):
        super(BertCrf, self).__init__()
        # config
        self.tag_to_ix = args.labels_map
        self.labels_num = args.labels_num
        self.hidden_size = args.hidden_size
        self.dropout = args.dropout
        # layers
        self.embedding = model.embedding
        self.encoder = model.encoder
        self.target = model.target
        self.dropout_layer = nn.Dropout(p=self.dropout)
        self.output_layer = nn.Linear(self.hidden_size, self.labels_num)
        self.crf = CRF(self.labels_num, batch_first=False)

    def forward(self, src, label, mask, pos=None, vm=None, padding_mask=None, batch_sequence_max_len=None):
        """
        Args:
            src: means token_ids  [batch_size x seq_length]
            label: means ner label_ids  [batch_size x seq_length]
            mask: [batch_size x seq_length]
        Returns:
            loss: Sequence labeling loss.
            correct: Number of labels that are predicted correctly.
            predict: Predicted label.
            label: Gold label.
        """
        # get batch_sequence_max_len
        if batch_sequence_max_len is None or batch_sequence_max_len <= 0:
            batch_sequence_max_len = src.shape[1]
        # reshape输入的数据->基于batch_sequence_max_len动态调整src/label/mask/pos/vm/padding_mask的长度，减少对无用padding的计算
        src = src[:, :batch_sequence_max_len]
        label = label[:, :batch_sequence_max_len]
        mask = mask[:, :batch_sequence_max_len]
        if pos is not None:
            pos = pos[:, :batch_sequence_max_len]
        if vm is not None:
            vm = vm[:, :batch_sequence_max_len, :batch_sequence_max_len]
        if padding_mask is not None:
            padding_mask = padding_mask[:, :batch_sequence_max_len]

        # batch_size
        bs = src.shape[0]

        # Embedding.
        emb = self.embedding(src, mask, pos)
        # Encoder.
        output = self.encoder(emb, mask, vm)
        # mission.
        output = self.dropout_layer(output)
        output = self.output_layer(output)

        if padding_mask is not None:
            crf_mask = padding_mask.transpose(0, 1).contiguous().byte()  # mask for crf (batch_sequence_max_len, batch_size)
        else:
            crf_mask = None
        # Get the emission scores from KBERT. shape is (seq_length, batch_size, num_tags)
        output = output.transpose(0, 1)

        # 转置label为(seq_length, batch_size)，适配crf的输入
        label_T = label.transpose(0, 1).contiguous()
        # Find the best path, and get the negative_log_likelihood loss according to tags
        loss, predict = self.crf(output, label_T, mask=crf_mask), self.crf.decode(output)
        # 取batch的avg loss
        loss = (-loss) / bs
        # list to tensor must be put into the same device
        predict = torch.LongTensor(predict).to(label.device)

        ### 计算correct
        # 将真实label转化为 1 * x 的矩阵， x表示token数量，也就是个一维的tensor
        label = label.contiguous().view(-1)
        # 同样拉直predict
        predict = predict.contiguous().view(-1)
        label_mask = (label > 0).float().to(label.device)  # label中的元素大于0则转换为1.0，即非padding字符对应的mask为1, padding对应0
        # view(-1) 表示 label_mask拉长为 1 * x 矩阵， x表示token数量
        label_mask = label_mask.contiguous().view(-1)
        predict = predict * label_mask.long()
        correct = torch.sum((predict.eq(label)).float())

        return loss, correct, predict, label

class BertSoftmax(nn.Module):
    def __init__(self, args, model):
        super(BertSoftmax, self).__init__()
        self.tag_to_ix = args.labels_map
        # 依次创建网络结构
        self.embedding = model.embedding
        self.encoder = model.encoder
        self.target = model.target
        self.labels_num = args.labels_num
        self.output_layer = nn.Linear(args.hidden_size, self.labels_num)
        self.softmax = nn.LogSoftmax(dim=-1)  # softmax在最后一维上sum为1，然后对softmax的结果取e为底的对数

    def forward(self, src, label, mask, pos=None, vm=None, padding_mask=None, batch_sequence_max_len=None):
        """
        Args:
            src: means token_ids  [batch_size x seq_length]
            label: means ner label_ids  [batch_size x seq_length]
            mask: [batch_size x seq_length]
        Returns:
            loss: Sequence labeling loss.
            correct: Number of labels that are predicted correctly.
            predict: Predicted label.
            label: Gold label.
        """
        # get batch_sequence_max_len
        if batch_sequence_max_len is None or batch_sequence_max_len <= 0:
            batch_sequence_max_len = src.shape[1]
        # reshape输入的数据-基于batch_sequence_max_len动态调整src/label/mask/pos/vm/padding_mask的长度，减少对无用padding的计算
        src = src[:, :batch_sequence_max_len]
        label = label[:, :batch_sequence_max_len]
        mask = mask[:, :batch_sequence_max_len]
        if pos is not None:
            pos = pos[:, :batch_sequence_max_len]
        if vm is not None:
            vm = vm[:, :batch_sequence_max_len, :batch_sequence_max_len]
        if padding_mask is not None:
            padding_mask = padding_mask[:, :batch_sequence_max_len]

        # Embedding.
        emb = self.embedding(src, mask, pos)
        # Encoder.
        output = self.encoder(emb, mask, vm)
        # mission.
        output = self.output_layer(output)
        # result
        # 通过softmax输出每个token对应各个ner label的概率
        output = self.softmax(output)

        ######
        # 拉直train data and label，计算loss
        # view(-1, self.labels_num)指将output转化为 batch token_nums * ner_label_nums 的矩阵
        output = output.contiguous().view(-1, self.labels_num)

        # 将真实label转化为 x * 1的矩阵， x表示token数量，直观上看就是一个label作为新矩阵的一行
        label = label.contiguous().view(-1, 1)
        label_mask = (label > 0).float().to(torch.device(label.device))  # label中的元素大于0则转换为1.0，即非padding字符对应的mask为1
        # one_hot：token_nums * ner_label_nums 的 one hot 矩阵，size与output相同，值为1指示真实标签
        # 先generate一个token_nums * ner_label_nums的全0张量，利用label在对应的位置打1，形成真实标签的one-hot encode
        one_hot = torch.zeros(label_mask.size(0), self.labels_num). \
            to(torch.device(label.device)). \
            scatter_(1, label, 1.0)

        # * 表示同位置元素相乘， 参数 1 表示按(token_nums, label_num)的label_num维度求和，返回1维的长度为token_nums的张量
        # example:
        # >> > import torch
        # >> > a = torch.tensor([[1, 2], [3, 4]])
        # >> > b = torch.tensor([[1, 0], [0, 1]])
        # >> > res = torch.sum(a * b, 1)
        # >> > res
        # tensor([1, 4])
        numerator = -torch.sum(output * one_hot, 1)  # output是经过logsoftmax的，因此numerator就是各个token组成的交叉熵张量
        # label_mask拉长为 1 * x 矩阵， x表示token数量
        label_mask = label_mask.contiguous().view(-1)
        # label拉长为 1 * x 矩阵， x表示token数量
        label = label.contiguous().view(-1)
        # 交叉熵张量乘以label_mask，表示去掉padding符的交叉熵，再求和得到整个batch的非padding字符的交叉熵
        numerator = torch.sum(label_mask * numerator)
        # 分母，表示真实值与真实值的交叉熵
        denominator = torch.sum(label_mask) + 1e-6
        loss = numerator / denominator

        predict = output.argmax(dim=-1)
        predict = predict * label_mask.long()
        correct = torch.sum(
            label_mask * (predict.eq(label)).float()
        )
        ### argmax example is below ###
        # >>> a = torch.tensor([[0.4, 0.6], [0.7, 0.3]])
        # >>> predict = a.argmax(dim=-1)
        # >>> predict
        # tensor([1, 0])
        # >>> b = torch.tensor([1, 1])
        # >>> res = predict.eq(b)
        # >>> res
        # tensor([True, False])
        # >>> a
        # tensor([[0.4000, 0.6000],
        #         [0.7000, 0.3000]])
        # >>> a_mask = (a > 0).float()
        # >>> a_mask
        # tensor([[1., 1.],
        #         [1., 1.]])

        return loss, correct, predict, label

class BertSoftmaxCross(nn.Module):
    def __init__(self, args, model):
        super(BertSoftmaxCross, self).__init__()
        self.tag_to_ix = args.labels_map
        self.labels_num = args.labels_num
        self.dropout = args.dropout
        # 依次创建网络结构
        self.embedding = model.embedding
        self.encoder = model.encoder
        self.target = model.target
        self.dropout_layer = nn.Dropout(p=self.dropout)
        self.output_layer = nn.Linear(args.hidden_size, self.labels_num)
        self.softmax = nn.LogSoftmax(dim=-1)  # softmax在最后一维上sum为1，然后对softmax的结果取e为底的对数

    def forward(self, src, label, mask, pos=None, vm=None, padding_mask=None, batch_sequence_max_len=None):
        """
        Args:
            src: means token_ids  [batch_size x seq_length]
            label: means ner label_ids  [batch_size x seq_length]
            mask: [batch_size x seq_length]
        Returns:
            loss: Sequence labeling loss.
            correct: Number of labels that are predicted correctly.
            predict: Predicted label.
            label: Gold label.
        """
        # get batch_sequence_max_len
        if batch_sequence_max_len is None or batch_sequence_max_len <= 0:
            batch_sequence_max_len = src.shape[1]
        # reshape输入的数据-基于batch_sequence_max_len动态调整src/label/mask/pos/vm/padding_mask的长度，减少对无用padding的计算
        src = src[:, :batch_sequence_max_len]
        label = label[:, :batch_sequence_max_len]
        mask = mask[:, :batch_sequence_max_len]
        if pos is not None:
            pos = pos[:, :batch_sequence_max_len]
        if vm is not None:
            vm = vm[:, :batch_sequence_max_len, :batch_sequence_max_len]
        if padding_mask is not None:
            padding_mask = padding_mask[:, :batch_sequence_max_len]

        # Embedding.
        emb = self.embedding(src, mask, pos)
        # Encoder.
        output = self.encoder(emb, mask, vm)
        # dropout
        output = self.dropout_layer(output)
        # mission.
        output = self.output_layer(output)
        # result
        # 通过softmax输出每个token对应各个ner label的概率
        output = self.softmax(output)

        ######
        # 拉直train data and label，计算loss
        # view(-1, self.labels_num)指将output转化为 batch token_nums * ner_label_nums 的矩阵
        output = output.contiguous().view(-1, self.labels_num)

        # 将真实label转化为 x * 1的矩阵， x表示token数量，直观上看就是一个label作为新矩阵的一行
        label = label.contiguous().view(-1, 1)
        label_mask = (label > 0).float().to(torch.device(label.device))  # label中的元素大于0则转换为1.0，即非padding字符对应的mask为1
        # one_hot：token_nums * ner_label_nums 的 one hot 矩阵，size与output相同，值为1指示真实标签
        # 先generate一个token_nums * ner_label_nums的全0张量，利用label在对应的位置打1，形成真实标签的one-hot encode
        # 这里scatter_(dim, index, src)将src中数据根据index中的索引按照dim的方向填进input中
        one_hot = torch.zeros(label_mask.size(0), self.labels_num). \
            to(torch.device(label.device)). \
            scatter_(1, label, 1.0)

        # label smooth
        epsilon = 0.1
        # 平滑后的标签有1-epsilon的概率来自于原分布，有epsilon的概率来自于均匀分布
        label_smooth = (1-epsilon) * one_hot + epsilon / self.labels_num

        # * 表示同位置元素相乘， 参数 1 表示按(token_nums, label_num)的label_num维度求和，返回1维的长度为token_nums的张量
        # example:
        # >> > import torch
        # >> > a = torch.tensor([[1, 2], [3, 4]])
        # >> > b = torch.tensor([[1, 0], [0, 1]])
        # >> > res = torch.sum(a * b, 1)
        # >> > res
        # tensor([1, 4])
        # numerator = -torch.sum(output * one_hot, 1)  # output是经过logsoftmax的，因此numerator就是各个token组成的交叉熵张量
        numerator = -torch.sum(output * label_smooth, 1)  # output是经过logsoftmax的，因此numerator就是各个token组成的交叉熵张量
        # label_mask拉长为 1 * x 矩阵， x表示token数量
        label_mask = label_mask.contiguous().view(-1)
        # label拉长为 1 * x 矩阵， x表示token数量
        label = label.contiguous().view(-1)
        # 交叉熵张量乘以label_mask，表示去掉padding符的交叉熵，再求和得到整个batch的非padding字符的交叉熵
        numerator = torch.sum(label_mask * numerator)
        loss = numerator

        predict = output.argmax(dim=-1)
        predict = predict * label_mask.long()
        correct = torch.sum(
            label_mask * (predict.eq(label)).float()
        )
        ### argmax example is below ###
        # >>> a = torch.tensor([[0.4, 0.6], [0.7, 0.3]])
        # >>> predict = a.argmax(dim=-1)
        # >>> predict
        # tensor([1, 0])
        # >>> b = torch.tensor([1, 1])
        # >>> res = predict.eq(b)
        # >>> res
        # tensor([True, False])
        # >>> a
        # tensor([[0.4000, 0.6000],
        #         [0.7000, 0.3000]])
        # >>> a_mask = (a > 0).float()
        # >>> a_mask
        # tensor([[1., 1.],
        #         [1., 1.]])

        return loss, correct, predict, label


acc_kg_dict = {
    0: [1,1,1,1,0,0],
    1: [1,1,1,1,1,1],
    2: [0.98533,0.98517,0.98590,0.98624,0.98688,0.98585],
    3: [0.87818,0.87282,0.88231,0.86805,0.86809,0.87434],
    4: [0.89344,0.90102,0.88881,0.87869,0.01930,0.89762],
    5: [0.88462,0.87578,0.88889,0.87248,0.89677,0.00045],
    6: [0.96354,0.97090,0.96158,0.96200,0.95277,0.96576],
    7: [0.89222,0.89464,0.89392,0.89001,0.89579,0.88770],
    8: [0.77818,0.78035,0.79439,0.78332,0.78519,0.77523],
    9: [0.94340,0.94398,0.94770,0.93555,0.93004,0.93776],
    10: [0.96077,0.96959,0.96393,0.96026,0.95541,0.96051],
    11: [0.85674,0.82337,0.81510,0.83967,0.82480,0.81365],
    12: [0.85248,0.82334,0.81789,0.82958,0.83155,0.81626],
    13: [0.85472,0.85795,0.85172,0.84560,0.86916,0.84409],
    14: [0.89683,0.89257,0.89007,0.89117,0.89334,0.88660],
}

acc_nokg_dict = {
    0: [1,1,1,1,0,0],
    1: [0,0,0,0,0,0],
    2: [0.98571,0.98653,0.98659,0.98576,0.98620,0.98573],
    3: [0.88591,0.86327,0.87706,0.88737,0.87621,0.86996],
    4: [0.89943,0.88605,0.88732,0.90263,0.02023,0.88975],
    5: [0.86875,0.86335,0.88961,0.86792,0.89865,0.00046],
    6: [0.95725,0.94825,0.96485,0.95400,0.95982,0.96095],
    7: [0.89157,0.89968,0.89509,0.88788,0.88656,0.88690],
    8: [0.78307,0.80165,0.78172,0.77495,0.77038,0.76587],
    9: [0.93971,0.94033,0.94387,0.94262,0.93131,0.93103],
    10: [0.96396,0.95938,0.97076,0.96706,0.96061,0.96042],
    11: [0.83288,0.82703,0.85000,0.82322,0.82927,0.84367],
    12: [0.83278,0.82845,0.83769,0.83369,0.83082,0.85495],
    13: [0.84672,0.86090,0.87669,0.84364,0.85045,0.84392],
    14: [0.88786,0.89774,0.90482,0.89281,0.88743,0.89149],

}



def getArgs():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # Path options. 配置各种路径
    parser.add_argument("--trained_model_path", default=None, type=str,
                        help="Path of the trained model.")
    parser.add_argument("--predict_output_path", default="./outputs/ner_predict.txt", type=str,
                        help="Path of the ner prediction.")
    parser.add_argument("--vocab_path", default="./models/google_vocab.txt", type=str,
                        help="Path of the vocabulary file.")
    parser.add_argument("--config_path", default="./models/google_config.json", type=str,
                        help="Path of the config file.")
    parser.add_argument("--predict_data_path", type=str, required=True,
                        help="Path of the predict dataset.")
    parser.add_argument("--train_path", type=str, required=True,
                        help="Path of the trainset.")

    # Model options.
    parser.add_argument("--batch_size", type=int, default=16,
                        help="Batch_size.")
    parser.add_argument("--seq_length", default=512, type=int,
                        help="Sequence length.")
    parser.add_argument("--encoder", choices=["bert", "lstm", "gru", \
                                              "cnn", "gatedcnn", "attn", \
                                              "rcnn", "crnn", "gpt", "bilstm"], \
                        default="bert", help="Encoder type.")
    parser.add_argument("--bidirectional", action="store_true", help="Specific to recurrent model.")

    # Subword options.
    parser.add_argument("--subword_type", choices=["none", "char"], default="none",
                        help="Subword feature type.")
    parser.add_argument("--sub_vocab_path", type=str, default="models/sub_vocab.txt",
                        help="Path of the subword vocabulary file.")
    parser.add_argument("--subencoder", choices=["avg", "lstm", "gru", "cnn"], default="avg",
                        help="Subencoder type.")
    parser.add_argument("--sub_layers_num", type=int, default=2, help="The number of subencoder layers.")

    # # Optimizer options.
    # parser.add_argument("--learning_rate", type=float, default=2e-5,
    #                     help="Learning rate.")
    # parser.add_argument("--warmup", type=float, default=0.1,
    #                     help="Warm up value.")

    # Training options.
    parser.add_argument("--dropout", type=float, default=0.1,
                        help="Dropout.")
    parser.add_argument("--seed", type=int, default=7,
                        help="Random seed.")

    # kg
    parser.add_argument("--kg_name", required=True, help="KG name or path")

    args = parser.parse_args()
    return args


def getLabeltoIx(train_path):
    # return a python dict, which is used to convert ner labels to ids

    # 创建供KBERT使用的label to id字典labels_map
    labels_map = {"[PAD]": 0, "[ENT]": 1}
    begin_ids = []

    # Find tagging labels 遍历训练集，找到全部的 ner label 并id化，然后找到每个B label对应的id
    with open(train_path, mode="r", encoding="utf-8") as f:
        for line_id, line in enumerate(f):
            if line_id == 0:
                continue
            labels = line.strip().split("\t")[1].split()
            for l in labels:
                if l not in labels_map:
                    if l.startswith("B") or l.startswith("S"):
                        begin_ids.append(len(labels_map))
                    labels_map[l] = len(labels_map)

    print("original Labels from Dataset: ", labels_map)
    return labels_map, begin_ids


def main():
    """
    main steps in this function:
    1.initialize args
    2.Build knowledge graph
    3.Build pretrain_model, and Load or initialize parameters for the model
    4.Build sequence labeling model base on pretrain_model, and try to use multiple GPUs, and load sequence labeling model to specified device
    5.define Dataset loader function, read_dataset function and evaluate function
    6.train, dev and eval
    """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    ###############
    # 1.初始化args #
    ###############
    args = getArgs()
    args.labels_map, args.begin_ids = getLabeltoIx(args.train_path)
    args.labels_num = len(args.labels_map)
    labels_map = args.labels_map

    # Load the hyperparameters of the config file. 加载bert_config.json
    args = load_hyperparam(args)

    # Load vocabulary. 加载vocab.txt
    vocab = Vocab()
    vocab.load(args.vocab_path)
    args.vocab = vocab
    ##################
    # 1.初始化args完毕 #
    ##################

    # 随机数seed，默认7
    set_seed(args.seed)

    ################
    # 2.Build knowledge graph.
    if args.kg_name == 'none':
        spo_files = []
    else:
        spo_files = [args.kg_name]
    # 加载知识库，返回实例
    kg = KnowledgeGraph(spo_files=spo_files, predicate=False, tokenizer_domain="medicine")

    ##### define Dataset loader function, read_dataset function and evaluate function #####
    # Dataset loader.
    def batch_loader(batch_size, input_ids, label_ids, mask_ids, pos_ids, vm_ids, tag_ids):
        instances_num = input_ids.size()[0]
        for i in range(instances_num // batch_size):
            input_ids_batch = input_ids[i*batch_size: (i+1)*batch_size, :]
            label_ids_batch = label_ids[i*batch_size: (i+1)*batch_size, :]
            mask_ids_batch = mask_ids[i*batch_size: (i+1)*batch_size, :]
            pos_ids_batch = pos_ids[i*batch_size: (i+1)*batch_size, :]
            vm_ids_batch = vm_ids[i*batch_size: (i+1)*batch_size, :, :]
            tag_ids_batch = tag_ids[i*batch_size: (i+1)*batch_size, :]
            yield input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch, tag_ids_batch
        if instances_num > instances_num // batch_size * batch_size:
            input_ids_batch = input_ids[instances_num//batch_size*batch_size:, :]
            label_ids_batch = label_ids[instances_num//batch_size*batch_size:, :]
            mask_ids_batch = mask_ids[instances_num//batch_size*batch_size:, :]
            pos_ids_batch = pos_ids[instances_num//batch_size*batch_size:, :]
            vm_ids_batch = vm_ids[instances_num//batch_size*batch_size:, :, :]
            tag_ids_batch = tag_ids[instances_num//batch_size*batch_size:, :]
            yield input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch, tag_ids_batch

    def batch_loader_bywxx(batch_size, input_ids, label_ids, mask_ids, pos_ids, vm_ids, tag_ids, padding_mask_ids):
        instances_num = input_ids.size()[0]
        for i in range(instances_num // batch_size):
            input_ids_batch = input_ids[i * batch_size: (i + 1) * batch_size, :]
            label_ids_batch = label_ids[i * batch_size: (i + 1) * batch_size, :]
            mask_ids_batch = mask_ids[i * batch_size: (i + 1) * batch_size, :]
            pos_ids_batch = pos_ids[i * batch_size: (i + 1) * batch_size, :]
            vm_ids_batch = vm_ids[i * batch_size: (i + 1) * batch_size, :, :]
            tag_ids_batch = tag_ids[i * batch_size: (i + 1) * batch_size, :]
            padding_mask_ids_batch = padding_mask_ids[i * batch_size: (i + 1) * batch_size, :]
            yield input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch, tag_ids_batch, padding_mask_ids_batch
        if instances_num > instances_num // batch_size * batch_size:
            input_ids_batch = input_ids[instances_num // batch_size * batch_size:, :]
            label_ids_batch = label_ids[instances_num // batch_size * batch_size:, :]
            mask_ids_batch = mask_ids[instances_num // batch_size * batch_size:, :]
            pos_ids_batch = pos_ids[instances_num // batch_size * batch_size:, :]
            vm_ids_batch = vm_ids[instances_num // batch_size * batch_size:, :, :]
            tag_ids_batch = tag_ids[instances_num // batch_size * batch_size:, :]
            padding_mask_ids_batch = padding_mask_ids[instances_num // batch_size * batch_size:, :]
            yield input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch, tag_ids_batch, padding_mask_ids_batch

    # Read dataset and convert. convert tokens to token_ids and labels to label_ids, and generate mask by [1] * len(token_ids)
    def read_dataset(path):
        dataset = []
        with open(path, mode="r", encoding="utf-8") as f:
            # 首行是header：text\tlabel，所以先读一行来去掉header
            f.readline()
            # tokens such as "呈 三 组 （ 5 / 1 3 个 ） 淋 巴 结 癌 转 移 。"
            # labels such as "O B-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy O O O O"
            tokens, labels = [], []
            for line_id, line in enumerate(f):
                tokens, labels = line.strip().split("\t")

                text = ''.join(tokens.split(" "))
                # 融入知识库，返回融合后的tokens, soft-pos, vm, tag
                tokens, pos, vm, tag = kg.add_knowledge_with_vm([text], add_pad=True, max_length=args.seq_length)
                tokens = tokens[0]
                pos = pos[0]
                vm = vm[0].astype("bool")
                tag = tag[0]

                # tokens to ids
                tokens = [vocab.get(t) for t in tokens]
                # ner labels to ids
                labels = [labels_map[l] for l in labels.split(" ")]
                mask = [1] * len(tokens)

                new_labels = []
                j = 0
                for i in range(len(tokens)):
                    if tag[i] == 0 and tokens[i] != PAD_ID:
                        new_labels.append(labels[j])
                        j += 1
                    elif tag[i] == 1 and tokens[i] != PAD_ID:  # tag[i] == 1表示是从知识库添加的实体，tokens[i] != PAD_ID表示不是padding符
                        new_labels.append(labels_map['[ENT]'])
                    else:
                        new_labels.append(labels_map[PAD_TOKEN])  # labels_map[PAD_TOKEN]为0，代表PAD填充

                # 每个样本用[tokens, new_labels, mask, pos, vm, tag]表示，其中tag用来区分原句子和引入的知识库实体，知识库实体用1表示，其他用0
                dataset.append([tokens, new_labels, mask, pos, vm, tag])
        # 打乱数据集
        random.shuffle(dataset)
        return dataset

    # 调用kg.add_knowledge_with_vm_bywxx，返回[[tokens, new_labels, seg_mask, pos, vm, tag, padding_mask], ...]
    def read_dataset_bywxx(path):
        dataset = []
        with open(path, mode="r", encoding="utf-8") as f:
            # 首行是header：text\tlabel，所以先读一行来去掉header
            f.readline()
            # tokens such as "呈 三 组 （ 5 / 1 3 个 ） 淋 巴 结 癌 转 移 。"
            # labels such as "O B-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy I-Anatomy O O O O"
            tokens, labels = [], []
            total_kg_entity_cnt = 0
            for line_id, line in enumerate(f):
                tokens, labels = line.strip().split("\t")

                text = ''.join(tokens.split(" "))
                # 融入知识库，返回融合后的tokens, soft-pos, vm, tag
                tokens, pos, vm, tag, padding_mask, entity_num = kg.add_knowledge_with_vm_bywxx([text],
                                                                                                max_length=args.seq_length)
                tokens = tokens[0]
                pos = pos[0]
                vm = vm[0].astype("bool")
                tag = tag[0]
                padding_mask = padding_mask[0]
                total_kg_entity_cnt += entity_num

                # tokens to ids
                tokens = [vocab.get(t) for t in tokens]
                # ner labels to ids
                labels = [labels_map[l] for l in labels.split(" ")]
                seg_mask = [1] * len(tokens)

                new_labels = []
                j = 0
                for i in range(len(tokens)):
                    if tag[i] == 0 and tokens[i] != PAD_ID:
                        new_labels.append(labels[j])
                        j += 1
                    elif tag[i] == 1 and tokens[i] != PAD_ID:  # tag[i] == 1表示是从知识库添加的实体，tokens[i] != PAD_ID表示不是padding符
                        new_labels.append(labels_map['[ENT]'])
                    else:
                        new_labels.append(labels_map[PAD_TOKEN])  # labels_map[PAD_TOKEN]为0，代表PAD填充

                # 每个样本用[tokens, new_labels, seg_mask, pos, vm, tag, padding_mask]表示，其中tag用来区分原句子和引入的知识库实体，知识库实体用1表示，其他用0
                dataset.append([tokens, new_labels, seg_mask, pos, vm, tag, padding_mask])

        print('add %s kg_entity to dataset' % str(total_kg_entity_cnt))
        # 打乱数据集
        random.shuffle(dataset)
        return dataset

    # ner ensemble predict function
    def ensemblePredict(args, models, target_size):
        '''

        :param args: an args instance returned by getArgs()
        :param models: a nn.ModuleList
        :return:
        '''
        dataset = read_dataset_bywxx(args.predict_data_path)
        input_ids = torch.LongTensor([sample[0] for sample in dataset])
        label_ids = torch.LongTensor([sample[1] for sample in dataset])
        mask_ids = torch.LongTensor([sample[2] for sample in dataset])
        pos_ids = torch.LongTensor([sample[3] for sample in dataset])
        vm_ids = torch.BoolTensor([sample[4] for sample in dataset])
        tag_ids = torch.LongTensor([sample[5] for sample in dataset])
        padding_mask_ids = torch.LongTensor([sample[6] for sample in dataset])

        instances_num = input_ids.size(0)
        batch_size = args.batch_size
        print('predict-ing')
        print("Batch size: ", batch_size)
        print("The number of predict instances:", instances_num)

        correct = 0  # 实体类别及边界正确
        tag_correct = 0  # 单个token的tag预测正确
        gold_entities_num = 0
        pred_entities_num = 0

        # 创建混淆矩阵
        confusion = torch.zeros(target_size, target_size, dtype=torch.long)

        # 对每个batch中每个sentence，对比实际的实体边界和预测的实体边界，单个实体边界完全一致则correct++
        for i_, (input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch, tag_ids_batch,
                padding_mask_ids_batch) in enumerate(
            batch_loader_bywxx(batch_size, input_ids, label_ids, mask_ids, pos_ids, vm_ids, tag_ids,
                               padding_mask_ids)):
            input_ids_batch = input_ids_batch.to(device)
            label_ids_batch = label_ids_batch.to(device)
            mask_ids_batch = mask_ids_batch.to(device)
            pos_ids_batch = pos_ids_batch.to(device)
            tag_ids_batch = tag_ids_batch.to(device)
            vm_ids_batch = vm_ids_batch.long().to(device)
            padding_mask_ids_batch = padding_mask_ids_batch.to(device)
            batch_true_max_len = int(torch.max(torch.sum(padding_mask_ids_batch, dim=1)).item())

            # ensemble逻辑
            all_pred = torch.LongTensor([]).to(device)
            for model in models:
                model.eval()
                # forward!!! pred预测值， gold真实值
                loss, _, pred, gold = model(input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch,
                                            padding_mask_ids_batch, batch_true_max_len)
                pred = pred.view(1, -1)
                all_pred = torch.cat((all_pred, pred), 0)

            if torch.cuda.is_available():
                # gpu tensor to python list
                all_pred = all_pred.cpu().numpy().tolist()
            else:
                # cpu tensor to python list
                all_pred = all_pred.numpy().tolist()

            ensemble_pred = []
            models_cnt = len(models)
            acc_dict = acc_kg_dict
            for index in range(len(all_pred[0])):
                d = dict()
                for r in range(models_cnt):
                    res = all_pred[r][index]
                    # 计数
                    if res not in d:
                        d[res] = 0
                    d[res] += 1
                    # d[res] += acc_dict[res][r]
                max_cnt = -float('inf')
                final_token_preds = []
                for k in d:
                    if d[k] > max_cnt:
                        final_token_preds = [k]
                        max_cnt = d[k]
                    elif d[k] == max_cnt:
                        final_token_preds.append(k)
                    else:
                        pass
                # 如果各个模型预测趋向一致
                if len(final_token_preds) == 1:
                    ensemble_pred.append(final_token_preds[0])
                # 如果各个模型预测势均力敌
                else:
                    ## 简单投票
                    # random_index = random.randint(0, len(final_token_preds)-1)
                    # ensemble_pred.append(final_token_preds[random_index])

                    # 老板话事
                    ensemble_pred.append(all_pred[2][index])

            pred = torch.LongTensor(ensemble_pred).to(device)
            # if i_ == 0:
            #     print('pred is ', pred.cpu().numpy().tolist())
            #     print('gold is ', gold.cpu().numpy().tolist())

            # 填充混淆矩阵
            for jj in range(pred.size()[0]):
                confusion[pred[jj], gold[jj]] += 1
            tag_correct += torch.sum(pred == gold).item()

            for j in range(gold.size()[0]):
                if gold[j].item() in args.begin_ids:
                    gold_entities_num += 1

            for j in range(pred.size()[0]):
                # 在非[PAD]字符下预测为实体起点
                if pred[j].item() in args.begin_ids and gold[j].item() != args.labels_map["[PAD]"]:
                    pred_entities_num += 1

            pred_entities_pos = []
            gold_entities_pos = []
            start, end = 0, 0

            # 查找真实实体的起点和终点
            for j in range(gold.size()[0]):
                if gold[j].item() in args.begin_ids:
                    start = j
                    for k in range(j + 1, gold.size()[0]):

                        if gold[k].item() == args.labels_map['[ENT]']:
                            continue

                        if gold[k].item() == args.labels_map["[PAD]"] or gold[k].item() == args.labels_map["O"] or gold[
                            k].item() in args.begin_ids:
                            end = k - 1
                            break
                    else:
                        end = gold.size()[0] - 1
                    # gold_entities_pos.append((start, end))
                    gold_entities_pos.append((start, end, gold[start].item()))  # wxx (实体起点，实体终点，实体类型)

            # 查找预测实体的起点和终点
            for j in range(pred.size()[0]):
                if pred[j].item() in args.begin_ids and gold[j].item() != args.labels_map["[PAD]"] and gold[j].item() != \
                        args.labels_map["[ENT]"]:
                    start = j
                    for k in range(j + 1, pred.size()[0]):

                        if gold[k].item() == args.labels_map['[ENT]']:
                            continue

                        if pred[k].item() == args.labels_map["[PAD]"] or pred[k].item() == args.labels_map["O"] or pred[
                            k].item() in args.begin_ids:
                            end = k - 1
                            break
                    else:
                        end = pred.size()[0] - 1
                    # pred_entities_pos.append((start, end))
                    pred_entities_pos.append((start, end, pred[start].item()))  # wxx (实体起点，实体终点，实体类型)

            # 预测实体的起点终点位置相同，视为正确
            for entity in pred_entities_pos:
                if entity not in gold_entities_pos:
                    continue
                else:
                    correct += 1

        print("Confusion matrix:")
        print(confusion)
        print("Report precision, recall, and f1:")
        for ii in range(confusion.size()[0]):
            # 完善代码，避免ZeroDivisionError: division by zero. wxx
            p_denominator = confusion[ii, :].sum().item()
            if p_denominator != 0:
                p = confusion[ii, ii].item() / p_denominator
            else:
                p = 0
            r_denominator = confusion[:, ii].sum().item()
            if r_denominator != 0:
                r = confusion[ii, ii].item() / r_denominator
            else:
                r = 0
            if p + r != 0:
                f1 = 2 * p * r / (p + r)
            else:
                f1 = 0
            # 注意，取label_1_f1 = f1只适用于2分类问题
            if ii == 1:
                label_1_f1 = f1
            print("Label {}: {:.5f}, {:.5f}, {:.5f}".format(ii, p, r, f1))

        print("correct, pred_entities_num, gold_entities_num are {:.5f}, {:.5f}, {:.5f}".format(correct,
                                                                                                pred_entities_num,
                                                                                                gold_entities_num))
        print("Report precision, recall, and f1:")
        if pred_entities_num == 0:
            p = 0
        else:
            p = correct / pred_entities_num
        if gold_entities_num == 0:
            r = 0
        else:
            r = correct / gold_entities_num
        if p + r == 0:
            f1 = 0
        else:
            f1 = 2 * p * r / (p + r)
        print("{:.5f}, {:.5f}, {:.5f}".format(p, r, f1))

        return f1

    # Evaluation function. return f1-value
    def evaluate(args, is_test):
        if is_test:
            dataset = read_dataset_bywxx(args.test_path)
        else:
            dataset = read_dataset_bywxx(args.dev_path)

        input_ids = torch.LongTensor([sample[0] for sample in dataset])
        label_ids = torch.LongTensor([sample[1] for sample in dataset])
        mask_ids = torch.LongTensor([sample[2] for sample in dataset])
        pos_ids = torch.LongTensor([sample[3] for sample in dataset])
        vm_ids = torch.BoolTensor([sample[4] for sample in dataset])
        tag_ids = torch.LongTensor([sample[5] for sample in dataset])
        padding_mask_ids = torch.LongTensor([sample[6] for sample in dataset])

        instances_num = input_ids.size(0)
        batch_size = args.batch_size

        if is_test:
            print('testing')
            print("Batch size: ", batch_size)
            print("The number of test instances:", instances_num)
        else:
            print('dev-ing')
            print("Batch size: ", batch_size)
            print("The number of dev instances:", instances_num)

        correct = 0  # 实体类别及边界正确
        tag_correct = 0  # 单个token的tag预测正确
        gold_entities_num = 0
        pred_entities_num = 0

        # 创建混淆矩阵
        confusion = torch.zeros(len(labels_map), len(labels_map), dtype=torch.long)

        # 不启用 BatchNormalization 和 Dropout，保证BN和dropout不发生变化
        # pytorch框架会自动把BN和Dropout固定住，不会取平均，而是用训练好的值，不然的话，一旦dev or test的batch_size过小，很容易就会被BN层影响结果。
        model.eval()

        # 对每个batch中每个sentence，对比实际的实体边界和预测的实体边界，单个实体边界完全一致则correct++
        for i, (input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch, tag_ids_batch,
                padding_mask_ids_batch) in enumerate(
                batch_loader_bywxx(batch_size, input_ids, label_ids, mask_ids, pos_ids, vm_ids, tag_ids,
                                   padding_mask_ids)):

            input_ids_batch = input_ids_batch.to(device)
            label_ids_batch = label_ids_batch.to(device)
            mask_ids_batch = mask_ids_batch.to(device)
            pos_ids_batch = pos_ids_batch.to(device)
            tag_ids_batch = tag_ids_batch.to(device)
            vm_ids_batch = vm_ids_batch.long().to(device)
            padding_mask_ids_batch = padding_mask_ids_batch.to(device)
            batch_true_max_len = int(torch.max(torch.sum(padding_mask_ids_batch, dim=1)).item())

            # forward!!!
            # loss, _, pred, gold = model(input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch)
            loss, _, pred, gold = model(input_ids_batch, label_ids_batch, mask_ids_batch, pos_ids_batch, vm_ids_batch,
                                        padding_mask_ids_batch, batch_true_max_len)
            # pred预测值， gold真实值

            # 填充混淆矩阵
            for jj in range(pred.size()[0]):
                confusion[pred[jj], gold[jj]] += 1
            tag_correct += torch.sum(pred == gold).item()

            for j in range(gold.size()[0]):
                if gold[j].item() in args.begin_ids:
                    gold_entities_num += 1

            for j in range(pred.size()[0]):
                # 在非[PAD]字符下预测为实体起点
                if pred[j].item() in args.begin_ids and gold[j].item() != args.labels_map["[PAD]"]:
                    pred_entities_num += 1

            pred_entities_pos = []
            gold_entities_pos = []
            start, end = 0, 0

            # 查找真实实体的起点和终点
            for j in range(gold.size()[0]):
                if gold[j].item() in args.begin_ids:
                    start = j
                    for k in range(j + 1, gold.size()[0]):

                        if gold[k].item() == args.labels_map['[ENT]']:
                            continue

                        if gold[k].item() == args.labels_map["[PAD]"] or gold[k].item() == args.labels_map["O"] or gold[
                            k].item() in args.begin_ids:
                            end = k - 1
                            break
                    else:
                        end = gold.size()[0] - 1
                    # gold_entities_pos.append((start, end))
                    gold_entities_pos.append((start, end, gold[start].item()))  # wxx (实体起点，实体终点，实体类型)

            # 查找预测实体的起点和终点
            for j in range(pred.size()[0]):
                if pred[j].item() in args.begin_ids and gold[j].item() != args.labels_map["[PAD]"] and gold[j].item() != \
                        args.labels_map["[ENT]"]:
                    start = j
                    for k in range(j + 1, pred.size()[0]):

                        if gold[k].item() == args.labels_map['[ENT]']:
                            continue

                        if pred[k].item() == args.labels_map["[PAD]"] or pred[k].item() == args.labels_map["O"] or pred[
                            k].item() in args.begin_ids:
                            end = k - 1
                            break
                    else:
                        end = pred.size()[0] - 1
                    # pred_entities_pos.append((start, end))
                    pred_entities_pos.append((start, end, pred[start].item()))  # wxx (实体起点，实体终点，实体类型)

            # 预测实体的起点终点位置相同，视为正确
            for entity in pred_entities_pos:
                if entity not in gold_entities_pos:
                    continue
                else:
                    correct += 1

        print("Confusion matrix:")
        print(confusion)
        print("Report precision, recall, and f1:")
        for ii in range(confusion.size()[0]):
            # 完善代码，避免ZeroDivisionError: division by zero. wxx
            p_denominator = confusion[ii, :].sum().item()
            if p_denominator != 0:
                p = confusion[ii, ii].item() / p_denominator
            else:
                p = 0
            r_denominator = confusion[:, ii].sum().item()
            if r_denominator != 0:
                r = confusion[ii, ii].item() / r_denominator
            else:
                r = 0
            if p + r != 0:
                f1 = 2 * p * r / (p + r)
            else:
                f1 = 0
            # 注意，取label_1_f1 = f1只适用于2分类问题
            if ii == 1:
                label_1_f1 = f1
            print("Label {}: {:.5f}, {:.5f}, {:.5f}".format(ii, p, r, f1))

        print("correct, pred_entities_num, gold_entities_num are {:.5f}, {:.5f}, {:.5f}".format(correct,
                                                                                                pred_entities_num,
                                                                                                gold_entities_num))
        print("Report precision, recall, and f1:")
        if pred_entities_num == 0:
            p = 0
        else:
            p = correct / pred_entities_num
        if gold_entities_num == 0:
            r = 0
        else:
            r = correct / gold_entities_num
        if p + r == 0:
            f1 = 0
        else:
            f1 = 2 * p * r / (p + r)
        print("{:.5f}, {:.5f}, {:.5f}".format(p, r, f1))

        return f1

    ################
    # 3.Build pretrain_model, and Load or initialize parameters for the model
    # A pseudo target is added.-->  args添加一个伪参数target，并被赋值bert，只有进行模型预训练时才会用到该参数
    args.target = "bert"
    # build_model function return a model, which consists of BertEmbedding, and 【encoder and target specified by args】
    model1 = build_model(args)
    model2 = build_model(args)
    model3 = build_model(args)
    model4 = build_model(args)
    model5 = build_model(args)
    model6 = build_model(args)

    trained_models_path = args.trained_model_path.split(',')
    print('trained_models_path is ', trained_models_path)
    trained_models_num = len(trained_models_path)

    # 注意，trained_models_path的顺序和长度必须和models的一致。且必须使用nn.ModuleList装载各个模型
    # init_models = nn.ModuleList([BertGruCrf(args, model), BertLstmCrf(args, model), BertCrf(args, model), BertSoftmax(args, model), BertLstm(args, model), BertGru(args, model)])
    init_models = [BertCrf(args, model1), BertGruCrf(args, model2), BertLstmCrf(args, model3), BertSoftmaxCross(args, model4), BertLstm(args, model5), BertGru(args, model6)]
    models = nn.ModuleList()
    for i in range(trained_models_num):
        # get ner model.
        model_ = init_models[i]
        # For simplicity, we use DataParallel wrapper to use multiple GPUs.
        if torch.cuda.device_count() > 1:
            print("{} GPUs are available. Let's use them.".format(torch.cuda.device_count()))
            model_ = nn.DataParallel(model_)
        model_ = model_.to(device)
        # 加载训练好的模型参数
        if hasattr(model_, 'module') and torch.cuda.device_count() > 1:
            model_.module.load_state_dict(torch.load(trained_models_path[i]))
        else:
            model_.load_state_dict(torch.load(trained_models_path[i]))
        models.append(model_)
    print('ensemblePredict start')
    ensemblePredict(args, models, len(labels_map))


if __name__ == "__main__":
    main()