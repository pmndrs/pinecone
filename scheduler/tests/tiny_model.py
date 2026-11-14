"""A tiny language model with random weights, for cache_live.py: it says nothing sensible, it only has to run on a
real llama.cpp server. Needs a checkout of llama.cpp (for its gguf-py and a vocabulary file) and a Python with numpy.

    python tiny_model.py <llama.cpp checkout> <out.gguf>
"""
import sys
repo, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo + "/gguf-py")
import numpy as np
import gguf

src = gguf.GGUFReader(repo + "/models/ggml-vocab-llama-spm.gguf")
n_embd, n_head, n_layer, n_ff = 64, 4, 2, 128

def field(name):
    f = src.fields[name]
    return f

tokens = [bytes(src.fields["tokenizer.ggml.tokens"].parts[i]).decode("utf-8", errors="replace") for i in src.fields["tokenizer.ggml.tokens"].data]
scores = [float(src.fields["tokenizer.ggml.scores"].parts[i][0]) for i in src.fields["tokenizer.ggml.scores"].data]
types = [int(src.fields["tokenizer.ggml.token_type"].parts[i][0]) for i in src.fields["tokenizer.ggml.token_type"].data]
n_vocab = len(tokens)

w = gguf.GGUFWriter(out, "llama")
w.add_name("tiny-random")
w.add_context_length(8192)
w.add_embedding_length(n_embd)
w.add_block_count(n_layer)
w.add_feed_forward_length(n_ff)
w.add_head_count(n_head)
w.add_head_count_kv(n_head)
w.add_layer_norm_rms_eps(1e-5)
w.add_rope_dimension_count(n_embd // n_head)
w.add_vocab_size(n_vocab)
w.add_file_type(0)
w.add_tokenizer_model("llama")
w.add_token_list(tokens)
w.add_token_scores(scores)
w.add_token_types(types)
for name, key in (("bos", "tokenizer.ggml.bos_token_id"), ("eos", "tokenizer.ggml.eos_token_id"), ("unk", "tokenizer.ggml.unknown_token_id")):
    if key in src.fields:
        v = int(src.fields[key].parts[src.fields[key].data[0]][0])
        getattr(w, f"add_{name}_token_id")(v)

rng = np.random.default_rng(7)
def t(name, *shape):
    w.add_tensor(name, (rng.standard_normal(shape) * 0.02).astype(np.float32))
t("token_embd.weight", n_vocab, n_embd)
for i in range(n_layer):
    w.add_tensor(f"blk.{i}.attn_norm.weight", np.ones(n_embd, dtype=np.float32))
    for k in ("attn_q", "attn_k", "attn_v", "attn_output"):
        t(f"blk.{i}.{k}.weight", n_embd, n_embd)
    w.add_tensor(f"blk.{i}.ffn_norm.weight", np.ones(n_embd, dtype=np.float32))
    t(f"blk.{i}.ffn_gate.weight", n_ff, n_embd)
    t(f"blk.{i}.ffn_down.weight", n_embd, n_ff)
    t(f"blk.{i}.ffn_up.weight", n_ff, n_embd)
w.add_tensor("output_norm.weight", np.ones(n_embd, dtype=np.float32))
t("output.weight", n_vocab, n_embd)
w.write_header_to_file()
w.write_kv_data_to_file()
w.write_tensors_to_file()
w.close()
print("wrote", out, n_vocab, "tokens")
