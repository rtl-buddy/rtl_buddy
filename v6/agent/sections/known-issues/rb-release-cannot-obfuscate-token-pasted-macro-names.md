## `rb release` cannot obfuscate token-pasted macro names

Verible renames the pieces of a token-pasted name (`` `define NXT(a) a``_nxt ``) separately, so the pasted result no longer matches its declaration, and a macro string quote is renamed while the string is not. `rb release` refuses both in any file it obfuscates. Rewrite the macro, exclude the file with `obfuscate: false` and a reason, or set `obfuscation.token-paste: preserve` to keep every name the paste can form.
