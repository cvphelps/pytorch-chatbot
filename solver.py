import os
import shutil
import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from tensorboardX import SummaryWriter
from tqdm import tqdm
from preprocess import Voc
from model import get_model 
from dataloader import get_loader
from evaluator import get_evaluator

class Solver():
    def __init__(self, args, config):
        self.args = args
        self.config = config
        self.set_model()
        USE_CUDA = torch.cuda.is_available() and not args.use_cpu
        self.device = torch.device("cuda" if USE_CUDA else "cpu")
        self.encoder.to(self.device)
        self.decoder.to(self.device)
        self.iteration = 0 
        self.best_valid_loss = 9e10 
        if self.args.load:
            self.load_checkpoint(self.args.load)
        self.mkdir()
        evaluators = self.config['solver']['evaluators']
        if args.test:
            self.test_loader = get_loader(config, 'test') 
            self.beam_size = self.config['solver']['beam_size']
            self.evaluators = [get_evaluator(e) for e in evaluators]
        else:
            self.train_loader = get_loader(config, 'train')
            self.valid_loader = get_loader(config, 'valid')
            self.log = SummaryWriter(self.config['solver']['log_dir'])
            self.n_iter     = self.config['solver']['n_iter']
            self.log_step   = self.config['solver']['log_step']
            self.valid_step = self.config['solver']['valid_step']
            self.evaluators = [get_evaluator(e, self.log) for e in evaluators]

    def get_optim(self, paras, ratio=1):
        use_apex = self.config['optimizer']['apex']
        optim_type = self.config['optimizer']['type']
        lr = self.config['optimizer']['lr'] * ratio
        if use_apex and optim_type == 'Adam':
            import apex
            return apex.optimizers.FusedAdam(paras, lr=lr)
        else:
            optim = getattr(torch.optim, optim_type)
            return optim(paras, lr=lr) 

    def set_model(self):
        print("=> Set Model")
        voc_size = self.config['preprocess']['size']
        self.encoder, self.decoder = get_model(voc_size, **self.config['model'])
        if self.args.multi_gpu:
            self.encoder = nn.DataParallel(self.encoder)
            self.decoder = nn.DataParallel(self.decoder)
        self.enc_opt = self.get_optim(self.encoder.parameters())
        self.dec_opt = self.get_optim(self.decoder.parameters(), self.config['optimizer']['decoder_learning_ratio'])
        self.voc = Voc()
        self.voc.load_file(os.path.join(self.config['preprocess']['save_dir'], 'voc.pkl'))

    def load_checkpoint(self, ckp_path):
        print("=> Loading Checkpoint '{}'".format(ckp_path))
        ckp = torch.load(ckp_path)
        self.iteration = ckp['iteration']
        self.best_valid_loss = ckp['best_valid_loss']
        self.encoder.load_state_dict(ckp['encoder'])
        self.decoder.load_state_dict(ckp['decoder'])
        self.enc_opt.load_state_dict(ckp['enc_opt'])
        self.dec_opt.load_state_dict(ckp['dec_opt'])

    def save_checkpoint(self, state, is_best, filename='checkpoint.pth.tar'):
        filename = os.path.join(self.save_dir, filename)
        torch.save(state, filename)
        if is_best:
            shutil.copyfile(filename, os.path.join(self.save_dir, 'model_best.pth.tar'))

    def mkdir(self):
        log_dir = self.config['solver']['log_dir']
        self.save_dir = self.config['solver']['save_dir']
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir)
        if not os.path.isdir(self.save_dir):
            os.makedirs(self.save_dir)

    def model_forward(self, input_seq, lens, target_seq, teacher_forcing_ratio=0.0):
        encoder_output, encoder_hidden = self.encoder(input_seq, lens, None)

        batch_size = input_seq.size(1)
        decoder_input = torch.LongTensor([[0 for _ in range(batch_size)]])
        decoder_input = decoder_input.to(self.device)

        # For bidirectional RNN, take uni-direction hidden state into consideration
        if self.config['model']['bidir']:
            decoder_hidden = encoder_hidden[:self.config['model']['n_layers']]

        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

        # Run through decoder one time step at a time
        max_target_len = target_seq.size(0) 
        decoder_output = torch.zeros(max_target_len, batch_size, self.config['preprocess']['size']).to(self.device)
        if use_teacher_forcing:
            for t in range(max_target_len):
                decoder_output[t,:,:], decoder_hidden, _ = self.decoder(
                    decoder_input, decoder_hidden, encoder_output
                )
                decoder_input = target_seq[t].view(1, -1) # Next input is current target
        else:
            for t in range(max_target_len):
                decoder_output[t,:,:], decoder_hidden, decoder_attn = self.decoder(
                    decoder_input, decoder_hidden, encoder_output
                )
                _, topi = decoder_output[t,:,:].topk(1) # [64, 1]

                decoder_input = torch.LongTensor([[topi[i][0] for i in range(batch_size)]])
                decoder_input = decoder_input.to(self.device)

        return decoder_output

    def index2word(self, tensor):
        index_list = tensor.permute(1, 0).tolist()
        tmp = []
        for sample in index_list:
            tmp1 = []
            for index in sample:
                if (index == 1) or (index == 2):
                    break
                tmp1.append(self.voc.index2word[index])
            tmp.append(' '.join(tmp1))
        return tmp

    def train(self):
        pbar = tqdm(total=self.n_iter)
        pbar.update(self.iteration)
        while(self.iteration < self.n_iter):
            for input_seq, target_seq, lens in self.train_loader:
                self.iteration += 1
                self.enc_opt.zero_grad()
                self.dec_opt.zero_grad()

                input_seq = input_seq.to(self.device)
                target_seq = target_seq.to(self.device)

                decoder_output = self.model_forward(input_seq, lens, target_seq, self.config['solver']['teacher_forcing_ratio'])

                loss = F.cross_entropy(decoder_output.permute(0, 2, 1), target_seq, ignore_index=2)
                loss.backward()

                clip = 50.0
                _ = torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), clip)
                _ = torch.nn.utils.clip_grad_norm_(self.decoder.parameters(), clip)

                self.enc_opt.step()
                self.dec_opt.step()

                if self.iteration % self.log_step == 0:
                    with torch.no_grad():
                        for evaluator in self.evaluators:
                            evaluator.cal(decoder_output, target_seq)
                            evaluator.log('train', self.iteration)

                if self.iteration % self.valid_step == 0:
                    with torch.no_grad():
                        self.valid()

                pbar.update(1)
                if self.iteration == self.n_iter:
                    break
        pbar.close()

    def valid(self):
        total_loss = 0
        for input_seq, target_seq, lens in self.valid_loader:
            input_seq = input_seq.to(self.device)
            target_seq = target_seq.to(self.device)
            decoder_output = self.model_forward(input_seq, lens, target_seq)
            loss = F.cross_entropy(decoder_output.permute(0, 2, 1), target_seq, ignore_index=2, reduction='sum')
            total_loss += loss
            for evaluator in self.evaluators:
                evaluator.cal(decoder_output, target_seq)
        for evaluator in self.evaluators:
            evaluator.log('valid', self.iteration)

        for input_seq, target_seq, lens in self.valid_loader:
            input_seq, target_seq, lens = input_seq[:, :10], target_seq[:, :10], lens[:10]
            input_seq = input_seq.to(self.device)
            target_seq = target_seq.to(self.device)
            decoder_output = self.model_forward(input_seq, lens, target_seq)
            _, topi = decoder_output.topk(1) # [64, 1]
            input_seq, target_seq, topi = map(self.index2word, (input_seq, target_seq, topi.squeeze()))
            for (i, t, o) in zip(input_seq, target_seq, topi):
                self.log.add_text('text',
                                  'A&nbsp;&nbsp;&nbsp;&nbsp;: {}  \nB&nbsp;&nbsp;&nbsp;&nbsp;:{}  \nAns:{}'.format(i, o, t),
                                  self.iteration)
            break

        is_best = True if total_loss < self.best_valid_loss else False
        self.best_valid_loss = min(total_loss, self.best_valid_loss)
        self.save_checkpoint({'iteration': self.iteration,
                              'best_valid_loss': self.best_valid_loss,
                              'encoder': self.encoder.state_dict(),
                              'decoder': self.decoder.state_dict(),
                              'enc_opt': self.enc_opt.state_dict(),
                              'dec_opt': self.dec_opt.state_dict()}, is_best)

    def test(self):
        self.load_checkpoint(os.path.join(self.save_dir, 'model_best.pth.tar'))
        filename = os.path.join(self.save_dir, 'testing_result.txt')
        f = open(filename, 'w')
        for input_seq, target_seq, lens in tqdm(self.test_loader):
            input_seq = input_seq.to(self.device)
            target_seq = target_seq.to(self.device)
            decoder_output = self.model_forward(input_seq, lens, target_seq)
            for evaluator in self.evaluators:
                evaluator.cal(decoder_output, target_seq)
            _, topi = decoder_output.topk(1) # [64, 1]
            input_seq, target_seq, topi = map(self.index2word, (input_seq, target_seq, topi.squeeze()))
            for (i, t, o) in zip(input_seq, target_seq, topi):
                f.write('i >> {}\n'.format(i))
                f.write('t >> {}\n'.format(t))
                f.write('o -- {}\n\n'.format(o))
        for evaluator in self.evaluators:
            score = evaluator.score
            print('{}: {}'.format(evaluator.name, score))
            f.write('{}: {}\n'.format(evaluator.name, score))
        f.close()
