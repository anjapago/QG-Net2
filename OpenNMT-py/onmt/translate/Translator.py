
import torch
from torch.autograd import Variable

import onmt.translate.Beam
import onmt.io

# for printing in console and debugging
from pdb import set_trace

class Translator(object):
    def __init__(self, model, fields,
                 beam_size, n_best,
                 max_length,
                 global_scorer, copy_attn, cuda,
                 beam_trace):
        self.model = model
        self.fields = fields
        self.n_best = n_best
        self.max_length = max_length
        self.global_scorer = global_scorer
        self.copy_attn = copy_attn
        self.beam_size = beam_size
        self.cuda = cuda

        # for debugging
        self.beam_accum = None
        if beam_trace:
            self.beam_accum = {
                "predicted_ids": [],
                "beam_parent_ids": [],
                "scores": [],
                "log_probs": []}

    def translate_batch(self, batch, data):
        # (0) Prep each of the components of the search.
        # And helper method for reducing verbosity.
        beam_size = self.beam_size
        batch_size = batch.batch_size
        data_type = data.data_type
        vocab = self.fields["tgt"].vocab
        beam = [onmt.translate.Beam(beam_size, n_best=self.n_best,
                                    cuda=self.cuda,
                                    global_scorer=self.global_scorer,
                                    pad=vocab.stoi[onmt.io.PAD_WORD],
                                    eos=vocab.stoi[onmt.io.EOS_WORD],
                                    bos=vocab.stoi[onmt.io.BOS_WORD])
                for __ in range(batch_size)]

        # Help functions for working with beams and batches
        def var(a): return Variable(a, volatile=True)
        #def var(a): return Variable(a, requires_grad=False)

        def rvar(a): return var(a.repeat(1, beam_size, 1))

        def bottle(m):
            return m.view(batch_size * beam_size, -1)

        def unbottle(m):
            return m.view(beam_size, batch_size, -1)

        # (1) Run the encoder on the src.
        src = onmt.io.make_features(batch, 'src', data_type)
        src_lengths = None
        if data_type == 'text':
            _, src_lengths = batch.src

        enc_states, context = self.model.encoder(src, src_lengths)
        dec_states = self.model.decoder.init_decoder_state(
                                        src, context, enc_states)
        # set_trace()

        if src_lengths is None:
            src_lengths = torch.Tensor(batch_size).type_as(context.data)\
                                                  .long()\
                                                  .fill_(context.size(0))

        # (2) Repeat src objects `beam_size` times.
        src_map = rvar(batch.src_map.data) if data_type == 'text' else None
        context = rvar(context.data)
        context_lengths = src_lengths.repeat(beam_size)
        dec_states.repeat_beam_size_times(beam_size)

        # (3) run the decoder to translate sentences, using beam search.
        for i in range(self.max_length):
            if all((b.done() for b in beam)):
                break

            # Construct batch x beam_size nxt words.
            # Get all the pending current beam words and arrange for forward.
            inp = var(torch.stack([b.get_current_state() for b in beam])
                      .t().contiguous().view(1, -1))

            # set_trace()

            # Turn any copied words to UNKs
            # 0 is unk
            if self.copy_attn:
                inp = inp.masked_fill(
                    inp.gt(len(self.fields["tgt"].vocab) - 1), 0)

            # set_trace()

            # Temporary kludge solution to handle changed dim expectation
            # in the decoder
            inp = inp.unsqueeze(2)

            # set_trace()

            # Run one step.
            dec_out, dec_states, attn = self.model.decoder(
                inp, context, dec_states, context_lengths=context_lengths)
            dec_out = dec_out.squeeze(0)
            # dec_out: beam x rnn_size

            # (b) Compute a vector of batch*beam word scores.
            if not self.copy_attn:
                out = self.model.generator.forward(dec_out).data
                out = unbottle(out)
                # beam x tgt_vocab
            else:
                out = self.model.generator.forward(dec_out,
                                                   attn["copy"].squeeze(0),
                                                   src_map)
                # beam x (tgt_vocab + extra_vocab)
                out = data.collapse_copy_scores(
                    unbottle(out.data),
                    batch, self.fields["tgt"].vocab)
                # beam x tgt_vocab
                out = out.log()

            # (c) Advance each beam.
            for j, b in enumerate(beam):
                b.advance(
                    out[:, j],
                    unbottle(attn["std"]).data[:, j, :context_lengths[j]])
                dec_states.beam_update(j, b.get_current_origin(), beam_size)

        # (4) Extract sentences from beam.
        ret = self._from_beam(beam)
        # set_trace()
        ret["gold_score"] = [0] * batch_size
        if "tgt" in batch.__dict__:
            ret["gold_score"] = self._run_target(batch, data)
        ret["batch"] = batch

        ####################
        # additional block of code for visualization (saliency)
        ####################
        # self.model.train()
        # get the state dicts (might be useful)
        state_dict = []
        tt = torch.cuda if self.cuda else torch
        for k, v in self.model.state_dict().items():
            state_dict.append((k.encode('utf-8'), v))

        # get one of the predictions
        selected_pred = ret['predictions'][0][0] # hard coded selection - assuming the first one has lowest ppl

        # turn the selected prediction into matrix that decoder can take for teacher forcing
        selected_pred = tt.LongTensor(selected_pred).view(len(selected_pred), 1, 1)

        # run the encoder to get the matrix of hidden states (for hook it up to get the gradients wrt outputs)
        enc_states, context = self.model.encoder(src, src_lengths)

        # run the decoder using "teacher forcing"
        # 1) init the decoder hidden states
        dec_states = self.model.decoder.init_decoder_state(src, context, enc_states)
        # 2) run one step
        # set_trace()
        inp = Variable(tt.LongTensor([self.fields['tgt'].vocab.stoi['<s>']]).unsqueeze(0).unsqueeze(2))
        all_saliency = tt.FloatTensor(src.size(0), selected_pred.size(0), dec_states.hidden[0].size(2))
        for i in range(len(selected_pred)):
            self.model.zero_grad()
            # Turn any copied words to UNKs
            # 0 is unk
            if self.copy_attn:
                inp = inp.masked_fill(inp.gt(len(self.fields["tgt"].vocab) - 1), 0)
            # run one step decoder
            # set_trace()
            dec_out, dec_states, attn = self.model.decoder(inp, context, dec_states, context_lengths=src_lengths)
            dec_out = dec_out.squeeze(0)
            # run generator
            if not self.copy_attn:
                out = self.model.generator.forward(dec_out)
                # beam x tgt_vocab
            else:
                out = self.model.generator.forward(dec_out, attn["copy"].squeeze(0), batch.src_map.data)
                # beam x (tgt_vocab + extra_vocab)
                out = data.collapse_copy_scores(out.unsqueeze(0), batch, self.fields["tgt"].vocab)
                out = out.squeeze(0)
            # calculate gradient/saliency
            # set_trace()
            selected_out = out[:, selected_pred[i]].sum()
            # selected_out.requires_grad = True
            # context.requires_grad = True
            saliency = torch.autograd.grad(selected_out, context, retain_graph=True)[0].data
            # store saliency to tensor
            all_saliency[:, i] = saliency.squeeze(1)
            # update next input using "teacher forcing"
            inp = Variable(tt.LongTensor([selected_pred[i]]).unsqueeze(0).unsqueeze(2))
            # visualize
            # set_trace()
            # # plt.figure(figsize=(20, 50))
            # plt.imshow(saliency.squeeze(1).cpu().numpy(), aspect='auto', cmap='Spectral')
            # plt.colorbar()
            # plt.yticks(np.arange(len(batch.dataset.examples[0].src)), batch.dataset.examples[0].src)
            # plt.show()

        ret['saliency'] = all_saliency
        ####################
        # end of visualization block
        ####################

        return ret

    def _from_beam(self, beam):
        ret = {"predictions": [],
               "scores": [],
               "attention": []}
        for b in beam:
            n_best = self.n_best
            scores, ks = b.sort_finished(minimum=n_best)
            hyps, attn = [], []
            for i, (times, k) in enumerate(ks[:n_best]):
                hyp, att = b.get_hyp(times, k)
                hyps.append(hyp)
                attn.append(att)
            ret["predictions"].append(hyps)
            ret["scores"].append(scores)
            ret["attention"].append(attn)
        return ret

    def _run_target(self, batch, data):
        data_type = data.data_type
        if data_type == 'text':
            _, src_lengths = batch.src
        else:
            src_lengths = None
        src = onmt.io.make_features(batch, 'src', data_type)
        tgt_in = onmt.io.make_features(batch, 'tgt')[:-1]

        #  (1) run the encoder on the src
        enc_states, context = self.model.encoder(src, src_lengths)
        dec_states = self.model.decoder.init_decoder_state(src,
                                                           context, enc_states)

        #  (2) if a target is specified, compute the 'goldScore'
        #  (i.e. log likelihood) of the target under the model
        tt = torch.cuda if self.cuda else torch
        gold_scores = tt.FloatTensor(batch.batch_size).fill_(0)
        dec_out, dec_states, attn = self.model.decoder(
            tgt_in, context, dec_states, context_lengths=src_lengths)

        tgt_pad = self.fields["tgt"].vocab.stoi[onmt.io.PAD_WORD]
        for dec, tgt in zip(dec_out, batch.tgt[1:].data):
            # Log prob of each word.
            out = self.model.generator.forward(dec)
            tgt = tgt.unsqueeze(1)
            scores = out.data.gather(1, tgt)
            scores.masked_fill_(tgt.eq(tgt_pad), 0)
            gold_scores += scores
        return gold_scores
