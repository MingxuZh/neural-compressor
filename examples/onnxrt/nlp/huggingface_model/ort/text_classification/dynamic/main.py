# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint:disable=redefined-outer-name,logging-format-interpolation

import logging
import argparse
import onnx
import onnxruntime as ort
import onnxruntime.quantization as ortq
import transformers
import os
import torch
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Union
import sys

from neural_compressor.data import DATALOADERS, DATASETS


class ONNXRTBertDataset:
    """Dataset used for model Bert.
    Args: data_dir (str): The input data dir.
          model_name_or_path (str): Path to pre-trained student model or shortcut name,
                                    selected in the list:
          max_seq_length (int, default=128): The maximum length after tokenization.
                                Sequences longer than this will be truncated,
                                sequences shorter will be padded.
          do_lower_case (bool, default=True): Whether to lowercase the input when tokenizing.
          task (str, default=mrpc): The name of the task to fine-tune.
                                    Choices include mrpc, qqp, qnli, rte,
                                    sts-b, cola, mnli, wnli.
          model_type (str, default='bert'): model type, support 'distilbert', 'bert',
                                            'mobilebert', 'roberta'.
          dynamic_length (bool, default=False): Whether to use fixed sequence length.
          evaluate (bool, default=True): Whether do evaluation or training.
          transform (transform object, default=None):  transform to process input data.
          filter (Filter objects, default=None): filter out examples according
                                                 to specific conditions.
    """
    def __init__(self, data_dir, model_name_or_path, max_seq_length=128,\
                do_lower_case=True, task='mrpc', model_type='bert', dynamic_length=False,\
                evaluate=True, transform=None, filter=None):
        task = task.lower()
        model_type = model_type.lower()
        assert task in ['mrpc', 'qqp', 'qnli', 'rte', 'sts-b', 'cola', \
            'mnli', 'wnli', 'sst-2'], 'Unsupported task type'
        assert model_type in ['distilbert', 'bert', 'mobilebert', 'roberta'], 'Unsupported \
            model type'
        self.dynamic_length = dynamic_length
        self.model_type = model_type
        self.max_seq_length = max_seq_length
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_name_or_path,
            do_lower_case=do_lower_case)
        self.dataset = load_and_cache_examples(data_dir, model_name_or_path, \
            max_seq_length, task, model_type, tokenizer, evaluate)

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, index):
        # return self.dataset[index]
        batch = tuple(t.detach().cpu().numpy() if not isinstance(t, np.ndarray) else t for t in self.dataset[index])
        return batch[:3], batch[-1]

def load_and_cache_examples(data_dir, model_name_or_path, max_seq_length, task, \
    model_type, tokenizer, evaluate):
    from torch.utils.data import TensorDataset

    processor = transformers.glue_processors[task]()
    output_mode = transformers.glue_output_modes[task]
    # Load data features from cache or dataset file
    if not os.path.exists("./dataset_cached"):
        os.makedirs("./dataset_cached")
    cached_features_file = os.path.join("./dataset_cached", 'cached_{}_{}_{}_{}'.format(
        'dev' if evaluate else 'train',
        list(filter(None, model_name_or_path.split('/'))).pop(),
        str(max_seq_length),
        str(task)))
    if os.path.exists(cached_features_file):
        logger.info("Load features from cached file {}.".format(cached_features_file))
        features = torch.load(cached_features_file)
    else:
        logger.info("Create features from dataset file at {}.".format(data_dir))
        label_list = processor.get_labels()
        examples = processor.get_dev_examples(data_dir) if evaluate else \
            processor.get_train_examples(data_dir)
        features = convert_examples_to_features(examples,
                                                tokenizer,
                                                task=task,
                                                label_list=label_list,
                                                max_length=max_seq_length,
                                                output_mode=output_mode,
        )
        logger.info("Save features into cached file {}.".format(cached_features_file))
        torch.save(features, cached_features_file)
    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_attention_mask = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
    all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
    all_seq_lengths = torch.tensor([f.seq_length for f in features], dtype=torch.long)
    if output_mode == "classification":
        all_labels = torch.tensor([f.label for f in features], dtype=torch.long)
    elif output_mode == "regression":
        all_labels = torch.tensor([f.label for f in features], dtype=torch.float)
    dataset = TensorDataset(all_input_ids, all_attention_mask, all_token_type_ids, \
        all_seq_lengths, all_labels)
    return dataset

def convert_examples_to_features(
    examples,
    tokenizer,
    max_length=128,
    task=None,
    label_list=None,
    output_mode="classification",
    pad_token=0,
    pad_token_segment_id=0,
    mask_padding_with_zero=True,
):
    processor = transformers.glue_processors[task]()
    if label_list is None:
        label_list = processor.get_labels()
        logger.info("Use label list {} for task {}.".format(label_list, task))
    label_map = {label: i for i, label in enumerate(label_list)}
    features = []
    for (ex_index, example) in enumerate(examples):
        inputs = tokenizer.encode_plus(
            example.text_a,
            example.text_b,
            add_special_tokens=True,
            max_length=max_length,
            return_token_type_ids=True,
            truncation=True,
        )
        input_ids, token_type_ids = inputs["input_ids"], inputs["token_type_ids"]
        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        attention_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

        # Zero-pad up to the sequence length.
        seq_length = len(input_ids)
        padding_length = max_length - len(input_ids)

        input_ids = input_ids + ([pad_token] * padding_length)
        attention_mask = attention_mask + \
            ([0 if mask_padding_with_zero else 1] * padding_length)
        token_type_ids = token_type_ids + ([pad_token_segment_id] * padding_length)

        assert len(input_ids) == max_length, \
            "Error with input_ids length {} vs {}".format(
            len(input_ids), max_length)
        assert len(attention_mask) == max_length, \
            "Error with attention_mask length {} vs {}".format(
            len(attention_mask), max_length
        )
        assert len(token_type_ids) == max_length, \
            "Error with token_type_ids length {} vs {}".format(
            len(token_type_ids), max_length
        )
        if output_mode == "classification":
            label = label_map[example.label]
        elif output_mode == "regression":
            label = float(example.label)
        else:
            raise KeyError(output_mode)

        feats = InputFeatures(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            label=label,
            seq_length=seq_length,
        )
        features.append(feats)
    return features

@dataclass(frozen=True)
class InputFeatures:
    """
    A single set of features of data.
    Property names are the same names as the corresponding inputs to a model.
    Args:
        input_ids: Indices of input sequence tokens in the vocabulary.
        attention_mask: Mask to avoid performing attention on padding token indices.
            Mask values selected in ``[0, 1]``: Usually ``1`` for tokens that are NOT MASKED,
            ``0`` for MASKED (padded) tokens.
        token_type_ids: (Optional) Segment token indices to indicate first and second
            portions of the inputs. Only some models use them.
        label: (Optional) Label corresponding to the input. Int for classification problems,
            float for regression problems.
        seq_length: (Optional) The length of input sequence before padding.
    """

    input_ids: List[int]
    attention_mask: Optional[List[int]] = None
    token_type_ids: Optional[List[int]] = None
    label: Optional[Union[int, float]] = None
    seq_length: Optional[List[int]] = None

class ONNXRTGLUE:
    """Computes GLUE score.

    Args:
        task (str, default=mrpc): The name of the task.
                                  Choices include mrpc, qqp, qnli, rte,
                                  sts-b, cola, mnli, wnli.

    """
    def __init__(self, task='mrpc'):
        assert task in ['mrpc', 'qqp', 'qnli', 'rte', 'sts-b', 'cola', \
            'mnli', 'wnli', 'sst-2'], 'Unsupported task type'
        self.pred_list = None
        self.label_list = None
        self.task = task
        self.return_key = {
            "cola": "mcc",
            "mrpc": "f1",
            "sts-b": "corr",
            "qqp": "acc",
            "mnli": "mnli/acc",
            "qnli": "acc",
            "rte": "acc",
            "wnli": "acc",
            "sst-2": "acc"
        }

    def update(self, preds, labels):
        """add preds and labels to storage"""
        if isinstance(preds, list) and len(preds) == 1:
            preds = preds[0]
        if isinstance(labels, list) and len(labels) == 1:
            labels = labels[0]
        if self.pred_list is None:
            self.pred_list = preds
            self.label_list = labels
        else:
            self.pred_list = np.append(self.pred_list, preds, axis=0)
            self.label_list = np.append(self.label_list, labels, axis=0)

    def reset(self):
        """clear preds and labels storage"""
        self.pred_list = None
        self.label_list = None

    def result(self):
        """calculate metric"""
        output_mode = transformers.glue_output_modes[self.task]

        if output_mode == "classification":
            processed_preds = np.argmax(self.pred_list, axis=1)
        elif output_mode == "regression":
            processed_preds = np.squeeze(self.pred_list)
        result = transformers.glue_compute_metrics(\
            self.task, processed_preds, self.label_list)
        return result[self.return_key[self.task]]

logger = logging.getLogger(__name__)
logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.WARN)

if __name__ == "__main__":
    logger.info('Evaluating ONNXRuntime full precision accuracy and performance:')
    parser = argparse.ArgumentParser(
    description='BERT fine-tune examples for classification/regression tasks.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--model_path',
        type=str,
        help="Pre-trained resnet50 model on onnx file"
    )
    parser.add_argument(
        '--benchmark',
        action='store_true', \
        default=False
    )
    parser.add_argument(
        '--tune',
        action='store_true', \
        default=False,
        help="whether quantize the model"
    )
    parser.add_argument(
       '--config',
       type=str,
       help="config yaml path"
    )
    parser.add_argument(
        '--output_model',
        type=str,
        default=None,
        help="output model path"
    )
    parser.add_argument(
        '--mode',
        type=str,
        help="benchmark mode of performance or accuracy"
    )
    parser.add_argument(
        '--data_path',
        type=str,
        help="input data path"
    )
    parser.add_argument(
        '--batch_size',
        default=8,
        type=int,
    )
    parser.add_argument(
        '--model_name_or_path',
        type=str,
        choices=['Intel/bert-base-uncased-mrpc',
                'Intel/roberta-base-mrpc',
                'Intel/xlm-roberta-base-mrpc',
                'Intel/camembert-base-mrpc',
                'distilbert-base-uncased-finetuned-sst-2-english',
                'Alireza1044/albert-base-v2-sst2',
                'philschmid/MiniLM-L6-H384-uncased-sst2',
                'Intel/MiniLM-L12-H384-uncased-mrpc',
                'bert-base-cased-finetuned-mrpc',
                'Intel/xlnet-base-cased-mrpc',
                'M-FAC/bert-mini-finetuned-mrpc',
                'Intel/electra-small-discriminator-mrpc',
                'Intel/bart-large-mrpc'],
        help="pretrained model name or path"
    )
    parser.add_argument(
        '--task',
        type=str,
        choices=['mrpc', 'qqp', 'qnli', 'rte', 'sts-b', 'cola', \
                'mnli', 'wnli', 'sst-2'],
        help="GLUE task name"
    )
    parser.add_argument(
        '--num_heads',
        default=12,
        type=int,
    )
    parser.add_argument(
        '--hidden_size',
        default=768,
        type=int,
    )
 
    args = parser.parse_args()

    dataset = ONNXRTBertDataset(data_dir=args.data_path,
                                model_name_or_path=args.model_name_or_path,
                                task=args.task)
    dataloader = DATALOADERS['onnxrt_integerops'](dataset, batch_size=args.batch_size)
    metric = ONNXRTGLUE(args.task)

    def eval_func(model, *args):
        metric.reset()
        import tqdm
        session = ort.InferenceSession(model.SerializeToString(), None)
        ort_inputs = {}
        len_inputs = len(session.get_inputs())
        inputs_names = [session.get_inputs()[i].name for i in range(len_inputs)]
        for idx, (inputs, labels) in enumerate(dataloader):
            if not isinstance(labels, list):
                labels = [labels]
            inputs = inputs[:len_inputs]
            for i in range(len_inputs):
                ort_inputs.update({inputs_names[i]: inputs[i]})
            predictions = session.run(None, ort_inputs)
            metric.update(predictions[0], labels)
        return metric.result()

    if args.benchmark:
        from neural_compressor.experimental import Benchmark, common
        model = onnx.load(args.model_path)
        if args.mode == 'performance':
            session = ort.InferenceSession(args.model_path, None)
            input_tensors = session.get_inputs()
            shape = []
            for i in range(len(input_tensors)):
                shape.append((1, 128))
            datasets = DATASETS('onnxrt_integerops')
            if args.model_name_or_path == 'Intel/bart-large-mrpc':
                dummy_dataset = datasets['dummy'](shape=shape, low=2, high=2, dtype='int64', label=True)
            else:
                dummy_dataset = datasets['dummy'](shape=shape, low=1, high=1, dtype='int64', label=True)
            evaluator = Benchmark(args.config)
            evaluator.model = common.Model(model)
            evaluator.b_dataloader = common.DataLoader(dummy_dataset)
            evaluator(args.mode)
        elif args.mode == 'accuracy':
            evaluator = Benchmark(args.config)
            evaluator.model = common.Model(model)
            evaluator.b_dataloader = dataloader
            evaluator.metric = metric
            evaluator.b_func = eval_func
            evaluator(args.mode)

    if args.tune:
        if args.model_name_or_path != 'Intel/bart-large-mrpc':
            from onnxruntime.transformers import optimizer
            from onnxruntime.transformers.onnx_model_bert import BertOptimizationOptions
            opt_options = BertOptimizationOptions('bert')
            opt_options.enable_embed_layer_norm = False

            model_optimizer = optimizer.optimize_model(
                args.model_path,
                'bert',
                num_heads=args.num_heads,
                hidden_size=args.hidden_size,
                optimization_options=opt_options)
            model = model_optimizer.model
            onnx.save(model, args.model_name_or_path.split('/')[-1] + '-optimized.onnx')

            ortq.quantize_dynamic(
                    args.model_name_or_path.split('/')[-1] + '-optimized.onnx',
                    args.output_model,
            )
        else:
            ortq.quantize_dynamic(
                    args.model_path,
                    args.output_model,
            )
            
        print('save ortq dynamic quantize model to', args.output_model)
        print('model which meet accuracy goal.')
        if os.path.exists(args.model_name_or_path.split('/')[-1] + '-optimized.onnx'):
            os.remove(args.model_name_or_path.split('/')[-1] + '-optimized.onnx')