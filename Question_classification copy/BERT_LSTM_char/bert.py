import torch
import torch.nn as nn
from pytorch_pretrained_bert.modeling import BertModel
from pytorch_pretrained_bert.tokenization import BertTokenizer
from torch.nn.utils.rnn import pad_sequence
import unicodedata
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as pad


def _is_whitespace(char):
    """Checks whether `chars` is a whitespace character."""
    # \t, \n, and \r are technically contorl characters but we treat them
    # as whitespace since they are generally considered as such.
    if char == " " or char == "\t" or char == "\n" or char == "\r":
        return True
    cat = unicodedata.category(char)
    if cat == "Zs":
        return True
    return False


def _is_control(char):
    """Checks whether `chars` is a control character."""
    # These are technically control characters but we count them as whitespace
    # characters.
    if char == "\t" or char == "\n" or char == "\r":
        return False
    cat = unicodedata.category(char)
    if cat.startswith("C"):
        return True
    return False


def _is_punctuation(char):
    """Checks whether `chars` is a punctuation character."""
    cp = ord(char)
    # We treat all non-letter/number ASCII as punctuation.
    # Characters such as "^", "$", and "`" are not in the Unicode
    # Punctuation class but we treat them as punctuation anyways, for
    # consistency.
    if (
        (cp >= 33 and cp <= 47)
        or (cp >= 58 and cp <= 64)
        or (cp >= 91 and cp <= 96)
        or (cp >= 123 and cp <= 126)
    ):
        return True
    cat = unicodedata.category(char)
    if cat.startswith("P"):
        return True
    return False


def _clean_text(text):
    output = []
    for char in text:
        cp = ord(char)
        if cp == 0 or cp == 0xFFFD or _is_control(char):
            continue
        if _is_whitespace(char):
            output.append(" ")
        else:
            output.append(char)
    return "".join(output)


def judge_ignore(word):
    if len(_clean_text(word)) == 0:
        return True
    for char in word:
        cp = ord(char)
        if cp == 0 or cp == 0xFFFD or _is_control(char):
            return True
    return False

def flatten(list_of_lists):
    for list in list_of_lists:
        for item in list:
            yield item

class Vocab(object):
    def __init__(self, bert_vocab_path):
        self.tokenizer = BertTokenizer.from_pretrained(
            bert_vocab_path, do_lower_case=False
        )

    def convert_tokens_to_ids(self, tokens):
        token_ids = self.tokenizer.convert_tokens_to_ids(tokens)
        ids = torch.tensor(token_ids, dtype=torch.long)
        mask = torch.ones(len(ids), dtype=torch.long)
        return ids, mask

    def subword_tokenize(self, tokens):
        subwords = list(map(self.tokenizer.tokenize, tokens))
        subword_lengths = [1] + list(map(len, subwords)) + [1]
        subwords = ["[CLS]"] + list(flatten(subwords)) + ["[SEP]"]
        token_start_idxs = torch.cumsum(torch.tensor([0] + subword_lengths[:-1]), dim=0)
        return subwords, token_start_idxs

    def subword_tokenize_to_ids(self, tokens):
        tokens = ["[PAD]" if judge_ignore(t) else t for t in tokens]
        subwords, token_start_idxs = self.subword_tokenize(tokens)
        subword_ids, mask = self.convert_tokens_to_ids(subwords)
        token_starts = torch.zeros(len(subword_ids), dtype=torch.uint8)
        token_starts[token_start_idxs] = 1
        return subword_ids, mask, token_starts

    def tokenize(self, tokens):
        subwords = list(map(self.tokenizer.tokenize, tokens))
        subword_lengths = [1] + list(map(len, subwords)) + [1]
        subwords = ["[CLS]"] + list(flatten(subwords)) + ["[SEP]"]
        return subwords

class Bert_Classifier(nn.Module):
    def __init__(self, data):
        super(Bert_Classifier, self).__init__()
        self.bert = BertModel.from_pretrained(data.bert_path)
        self.args = data
        self.use_syntax = data.use_syntax
        if self.use_syntax:
            self.syntax_embed = nn.Embedding(data.len_syntax_dict, data.syntax_dim)
            self.lstm = nn.LSTM(data.bert_embedding_size+data.syntax_dim, data.hidden_dim, num_layers=data.lstm_layer, batch_first=True, bidirectional=data.bilstm)
        else:
            self.lstm = nn.LSTM(data.bert_embedding_size, data.hidden_dim, num_layers=data.lstm_layer, batch_first=True, bidirectional=data.bilstm)
        
        if data.bilstm:
            self.linear = nn.Linear(data.hidden_dim*2, data.num_labels)
        else:
            self.linear = nn.Linear(data.hidden_dim, data.num_labels)
        self.dropout = nn.Dropout(data.dropout)
     
    def forward(self, input_idxs, input_masks, syntax_ids=None):
        bert_outs, _ = self.bert(
            input_idxs,
            token_type_ids=None,
            attention_mask=input_masks,
            output_all_encoded_layers=False,
        )
        
        lens = torch.sum(input_idxs.gt(0), dim=1)
        # bert_outs = torch.split(bert_outs[token_start], lens.tolist())
        bert_outs = pad_sequence(bert_outs, batch_first=True)
        lstm_input = bert_outs
        if self.use_syntax:
            syntax_vec = self.syntax_embed(syntax_ids)
            lstm_input = torch.cat((lstm_input, syntax_vec),-1)

        max_len = lstm_input.size(1)
        lstm_input = lstm_input[:, :max_len, :]
        # mask = torch.arange(max_len).cuda() < lens.unsqueeze(-1)
        # add lstm after bert
        sorted_lens, sorted_idx = torch.sort(lens, dim=0, descending=True)
        reverse_idx = torch.sort(sorted_idx, dim=0)[1]
        lstm_input = lstm_input[sorted_idx]
        lstm_input = pack(lstm_input, sorted_lens, batch_first=True)
        lstm_output, (h, _) = self.lstm(lstm_input)

        hidden = torch.cat((h[-1, :, :], h[-2, :, :]), -1)

        hidden = hidden[reverse_idx]
        out = self.linear(torch.tanh(hidden))
        return out

    def neg_log_likehood(self, subword_idxs, subword_masks, token_start, batch_label):
        bert_outs, _ = self.bert(
            subword_idxs,
            token_type_ids=None,
            attention_mask=subword_masks,
            output_all_encoded_layers=False,
        )
        lens = token_start.sum(dim=1)

        #x = bert_outs[token_start]
        bert_outs = torch.split(bert_outs[token_start], lens.tolist())
        bert_outs = pad_sequence(bert_outs, batch_first=True)
        max_len = bert_outs.size(1)
        mask = torch.arange(max_len).cuda() < lens.unsqueeze(-1)
        
        
        # add lstm after bert
        sorted_lens, sorted_idx = torch.sort(lens, dim=0, descending=True)
        reverse_idx = torch.sort(sorted_idx, dim=0)[1]
        bert_outs = bert_outs[sorted_idx]
        bert_outs = pack(bert_outs, sorted_lens, batch_first=True)
        bert_outs, hidden = self.lstm(bert_outs)
        bert_outs, _ = pad(bert_outs, batch_first=True)
        bert_outs = bert_outs[reverse_idx]
        

        out = self.linear(bert_outs)
        batch_size = out.size(0)
        seq_len = out.size(1)
        out = out.view(-1, out.size(2))
        score = torch.nn.functional.log_softmax(out, 1)
        loss_function =  nn.NLLLoss(ignore_index=0, reduction="sum")
        loss = loss_function(score, batch_label.view(-1))
        _, seq = torch.max(score, 1)
        seq = seq.view(batch_size, seq_len)
        if self.args.average_loss:
            loss = loss / mask.float().sum()
        return loss,seq

    def extract_feature(self,subword_idxs,subword_masks,token_start,batch_label,layers):
        out_layers,outs = [],[]
        bert_outs, _ = self.bert(
            subword_idxs,
            token_type_ids=None,
            attention_mask=subword_masks,
            output_all_encoded_layers=True,
        )
        lens = token_start.sum(dim=1)
        #x = bert_outs[token_start]
        #bert_outs = torch.split(bert_outs[token_start].cpu(), lens.tolist())
        for layer in layers:
            out_layers.append(torch.split(bert_outs[layer][token_start].cpu(), lens.tolist()))
        batch_size = subword_idxs.size(0)
        for idx in range(batch_size):
            items = []
            for idy,item in enumerate(out_layers):
                items.append(item[idx].unsqueeze(1))
            outs.append(torch.cat(items,dim=1))
        return outs        

class Bert_Classifier_Pooling(nn.Module):
    def __init__(self, data):
        super(Bert_Classifier, self).__init__()
        self.bert = BertModel.from_pretrained(data.bert_path)
        self.args = data
        self.use_syntax = data.use_syntax
        if self.use_syntax:
            self.syntax_embed = nn.Embedding(data.len_syntax_dict, data.syntax_dim)
            self.lstm = nn.LSTM(data.bert_embedding_size+data.syntax_dim, data.hidden_dim, num_layers=data.lstm_layer, batch_first=True, bidirectional=data.bilstm)
        else:
            self.lstm = nn.LSTM(data.bert_embedding_size, data.hidden_dim, num_layers=data.lstm_layer, batch_first=True, bidirectional=data.bilstm)
        
        if data.bilstm:
            self.linear = nn.Linear(data.hidden_dim*2, data.num_labels)
        else:
            self.linear = nn.Linear(data.hidden_dim, data.num_labels)
        self.dropout = nn.Dropout(data.dropout)
     
    def forward(self, input_idxs, input_masks, syntax_ids=None):
        bert_outs, _ = self.bert(
            input_idxs,
            token_type_ids=None,
            attention_mask=input_masks,
            output_all_encoded_layers=False,
        )
        lens = torch.sum(input_idxs.gt(0), dim=1)
        # bert_outs = torch.split(bert_outs[token_start], lens.tolist())
        bert_outs = pad_sequence(bert_outs, batch_first=True)
        lstm_input = bert_outs
        if self.use_syntax:
            syntax_vec = self.syntax_embed(syntax_ids)
            lstm_input = torch.cat((lstm_input, syntax_vec),-1)

        max_len = lstm_input.size(1)
        lstm_input = lstm_input[:, :max_len, :]
        # mask = torch.arange(max_len).cuda() < lens.unsqueeze(-1)
        # add lstm after bert
        sorted_lens, sorted_idx = torch.sort(lens, dim=0, descending=True)
        reverse_idx = torch.sort(sorted_idx, dim=0)[1]
        lstm_input = lstm_input[sorted_idx]
        lstm_input = pack(lstm_input, sorted_lens, batch_first=True)
        lstm_output, (h, _) = self.lstm(lstm_input) # lstm_output:[batch,sequence_length,embeding]
        output, _ = pad(lstm_output, batch_first=True)
        output = lstm_output.permute(0, 2, 1) # lstm_output:[batch,embeding,sequence_length]
        
        output = nn.MaxPool1d(output, output.size()[2]) # lstm_output:[batch,embeding,1]
        output = output.squeeze(2) # lstm_output:[batch,embeding]
        output = output[reverse_idx]
        output = self.linear(output)
        out = self.linear(torch.tanh(output))
        return out