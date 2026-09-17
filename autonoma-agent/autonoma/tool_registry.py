"""Adaptadores de herramientas; separados del bucle de conversación."""
from __future__ import annotations
from typing import Any
from autonoma.tool_contracts import LOCAL_TOOLS


class ToolRegistry:
    def __init__(self, search: Any, fs: Any) -> None:
        self.search = search
        self.fs = fs
        self.handlers = {
            'web_search': self._research,
            'fetch_url': self._fetch,
            'save_knowledge': self._save,
            'read_knowledge': self._read,
            'list_knowledge': self._list,
        }
        for name in LOCAL_TOOLS:
            self.handlers[name] = self._local

    def execute(self, name: str, args: dict[str, Any], *, user_prompt: str = '') -> str:
        return self.handlers[name](name, args, user_prompt)

    def _research(self, name: str, args: dict[str, Any], prompt: str) -> str:
        return self.search.format_bundle(self.search.research(
            args['query'], fetch_pages=args.get('fetch_pages'), save=True))

    def _fetch(self, name: str, args: dict[str, Any], prompt: str) -> str:
        text = self.search.fetch_url(args['url'])
        # Una descarga no requiere una escritura duplicada de conocimiento.
        return text[:16_000]

    def _save(self, name: str, args: dict[str, Any], prompt: str) -> str:
        return f"Guardado {self.search.save_note(args['title'], args['content'])}"

    def _read(self, name: str, args: dict[str, Any], prompt: str) -> str:
        try:
            return self.search.read_note(args['query'])
        except FileNotFoundError:
            return self.search.search_notes(args['query'])

    def _list(self, name: str, args: dict[str, Any], prompt: str) -> str:
        files = self.search.list_notes(50)
        return '\n'.join(f'- {p.name}' for p in files) or 'knowledge_base vacía'

    def _local(self, name: str, args: dict[str, Any], prompt: str) -> str:
        if name in {'write_file', 'copy_path', 'move_path', 'delete_path', 'mkdir'}:
            args = {**args, 'user_prompt': prompt}
        result = getattr(self.fs, name)(**args)
        return self.fs.format_command_result(result) if name == 'run_command' else result
