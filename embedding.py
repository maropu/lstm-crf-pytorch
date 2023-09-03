from utils import *

class embed(nn.Module):

    def __init__(self, ls, cti, wti, batch_first = False, hre = False):

        super().__init__()
        self.dim = sum(ls.values())
        self.batch_first = batch_first

        # architecture
        self.char_embed = None
        self.word_embed = None
        self.sent_embed = None

        for model, dim in ls.items():
            assert model in ("lookup", "cnn", "rnn", "sae")
            if model in ("cnn", "rnn"):
                self.char_embed = getattr(self, model)(len(cti), dim)
            if model in ("lookup", "sae"):
                self.word_embed = getattr(self, model)(len(wti), dim)

        if hre:
            self.sent_embed = self.rnn(self.dim, self.dim, hre = True)

        self = self.cuda() if CUDA else self

    def forward(self, b, xc, xw):

        hc, hw = None, None

        if self.char_embed:
            hc = self.char_embed(xc) # [Ls, B * Ld, Lw] -> [Ls, B * Ld, Hc]
        if self.word_embed:
            hw = self.word_embed(xw) # [Ls, B * Ld] -> [Ls, B * Ld, Hw]

        h = torch.cat([h for h in [hc, hw] if type(h) == torch.Tensor], 2)

        if self.sent_embed:
            if self.batch_first:
                h.transpose_(0, 1)
            h = self.sent_embed(h) # [Lw, B * Ld, H] -> [1, B * Ld, H]
            h = h.view(-1, b, h.size(2)) # [Ld, B, H]
            if self.batch_first:
                h.transpose_(0, 1)

        return h

    class lookup(nn.Module):

        def __init__(self, vocab_size, embed_size):

            super().__init__()
            self.embed = nn.Embedding(vocab_size, embed_size, padding_idx = PAD_IDX)

        def forward(self, x):

            return self.embed(x) # [Ls, B * Ld, H]

    class cnn(nn.Module):

        def __init__(self, vocab_size, embed_size):

            super().__init__()
            dim = 50
            num_featmaps = 50 # feature maps generated by each kernel
            kernel_sizes = [3]

            # architecture
            self.embed = nn.Embedding(vocab_size, dim, padding_idx = PAD_IDX)
            self.conv = nn.ModuleList([nn.Conv2d(
                in_channels = 1, # Ci
                out_channels = num_featmaps, # Co
                kernel_size = (i, dim) # height, width
            ) for i in kernel_sizes]) # num_kernels (K)
            self.dropout = nn.Dropout(DROPOUT)
            self.fc = nn.Linear(len(kernel_sizes) * num_featmaps, embed_size)

        def forward(self, x):

            b = x.size(1) # B' = B * Ld
            x = x.reshape(-1, x.size(2)) # [B' * Ls, Lw]
            x = self.embed(x).unsqueeze(1) # [B' * Ls, Ci = 1, Lw, dim]
            h = [conv(x) for conv in self.conv] # [B' * Ls, Co, Lw, 1] * K
            h = [F.relu(k).squeeze(3) for k in h] # [B' * Ls, Co, Lw] * K
            h = [F.max_pool1d(k, k.size(2)).squeeze(2) for k in h] # [B' * Ls, Co] * K
            h = torch.cat(h, 1) # [B' * Ls, Co * K]
            h = self.dropout(h)
            h = self.fc(h) # fully connected layer [B' * Ls, H]
            h = h.view(-1, b, h.size(1)) # [Ls, B', H]

            return h

    class rnn(nn.Module):

        def __init__(self, vocab_size, embed_size, hre = False):

            super().__init__()
            self.dim = embed_size
            self.rnn_type = "GRU" # LSTM, GRU
            self.num_dirs = 2 # unidirectional: 1, bidirectional: 2
            self.num_layers = 2
            self.hre = hre

            # architecture
            self.embed = nn.Embedding(vocab_size, embed_size, padding_idx = PAD_IDX)
            self.rnn = getattr(nn, self.rnn_type)(
                input_size = self.dim,
                hidden_size = self.dim // self.num_dirs,
                num_layers = self.num_layers,
                bias = True,
                dropout = DROPOUT,
                bidirectional = (self.num_dirs == 2)
            )

        def init_state(self, b): # initialize RNN states

            n = self.num_layers * self.num_dirs
            h = self.dim // self.num_dirs
            hs = zeros(n, b, h) # hidden state
            if self.rnn_type == "GRU":
                return hs
            cs = zeros(n, b, h) # LSTM cell state
            return (hs, cs)

        def forward(self, x):

            b = x.size(1) # B' = B * Ld
            s = self.init_state(b * (1 if self.hre else x.size(0)))
            if not self.hre: # [Ls, B', Lw] -> [Lw, B' * Ls, H]
                x = x.reshape(-1, x.size(2)).transpose(0, 1)
                x = self.embed(x)

            h, s = self.rnn(x, s)
            h = s if self.rnn_type == "GRU" else s[-1]
            h = torch.cat([x for x in h[-self.num_dirs:]], 1) # final hidden state
            h = h.view(-1, b, h.size(1)) # [Ls, B', H]

            return h

    class sae(nn.Module): # self-attentive encoder

        def __init__(self, vocab_size, embed_size = 512):

            super().__init__()
            dim = embed_size
            num_layers = 1

            # architecture
            self.embed = nn.Embedding(vocab_size, dim, padding_idx = PAD_IDX)
            self.pe = self.positional_encoding(dim)
            self.layers = nn.ModuleList([self.layer(dim) for _ in range(num_layers)])

        def forward(self, x):

            mask = x.eq(PAD_IDX).view(x.size(0), 1, 1, -1)
            x = self.embed(x)
            h = x + self.pe[:x.size(1)]
            for layer in self.layers:
                h = layer(h, mask)

            return h

        def positional_encoding(self, dim, maxlen = 1000): # positional encoding

            pe = Tensor(maxlen, dim)
            pos = torch.arange(0, maxlen, 1.).unsqueeze(1)
            k = torch.exp(-np.log(10000) * torch.arange(0, dim, 2.) / dim)
            pe[:, 0::2] = torch.sin(pos * k)
            pe[:, 1::2] = torch.cos(pos * k)

            return pe

        class layer(nn.Module): # encoder layer

            def __init__(self, dim):

                super().__init__()

                # architecture
                self.attn = embed.sae.attn_mh(dim)
                self.ffn = embed.sae.ffn(dim)

            def forward(self, x, mask):

                z = self.attn(x, x, x, mask)
                z = self.ffn(z)

                return z

        class attn_mh(nn.Module): # multi-head attention

            def __init__(self, dim):

                super().__init__()
                self.D = dim # dimension of model
                self.H = 8 # number of heads
                self.Dk = self.D // self.H # dimension of key
                self.Dv = self.D // self.H # dimension of value

                # architecture
                self.Wq = nn.Linear(self.D, self.H * self.Dk) # query
                self.Wk = nn.Linear(self.D, self.H * self.Dk) # key for attention distribution
                self.Wv = nn.Linear(self.D, self.H * self.Dv) # value for context representation
                self.Wo = nn.Linear(self.H * self.Dv, self.D)
                self.dropout = nn.Dropout(DROPOUT)
                self.norm = nn.LayerNorm(self.D)

            def attn_sdp(self, q, k, v, mask): # scaled dot-product attention

                c = np.sqrt(self.Dk) # scale factor
                a = torch.matmul(q, k.transpose(2, 3)) / c # compatibility function
                a = a.masked_fill(mask, -10000)
                a = F.softmax(a, 2)
                a = torch.matmul(a, v)

                return a # attention weights

            def forward(self, q, k, v, mask):

                b = q.size(0)
                x = q # identity
                q = self.Wq(q).view(b, -1, self.H, self.Dk).transpose(1, 2)
                k = self.Wk(k).view(b, -1, self.H, self.Dk).transpose(1, 2)
                v = self.Wv(v).view(b, -1, self.H, self.Dv).transpose(1, 2)
                z = self.attn_sdp(q, k, v, mask)
                z = z.transpose(1, 2).contiguous().view(b, -1, self.H * self.Dv)
                z = self.Wo(z)
                z = self.norm(x + self.dropout(z)) # residual connection and dropout

                return z

        class ffn(nn.Module): # position-wise feed-forward networks

            def __init__(self, dim):

                super().__init__()
                dim_ffn = 2048

                # architecture
                self.layers = nn.Sequential(
                    nn.Linear(dim, dim_ffn),
                    nn.ReLU(),
                    nn.Dropout(DROPOUT),
                    nn.Linear(dim_ffn, dim)
                )
                self.norm = nn.LayerNorm(dim)

            def forward(self, x):

                z = x + self.layers(x) # residual connection
                z = self.norm(z) # layer normalization

                return z
