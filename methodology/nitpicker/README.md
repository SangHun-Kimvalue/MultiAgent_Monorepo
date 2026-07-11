# Nitpicker Wrapper

This folder is copied into target projects by `install.sh`.

Default provider is Ollama:

```bash
ollama list
ollama pull qwen2.5-coder:7b
python3 nitpicker/run_nit.py --self-test
python3 nitpicker/run_nit.py --changed
```

Dry run without a local model:

```bash
python3 nitpicker/run_nit.py --provider mock --changed
```

Rules:
- Use `run_nit.py` as the only entrypoint.
- Do not pass raw `git diff` text through shell arguments.
- Keep secrets and real provider tokens out of this repo.
- Edit `nitpicker.config.json`, not the example file, for local project settings.
