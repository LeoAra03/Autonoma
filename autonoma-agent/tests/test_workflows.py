from types import SimpleNamespace

import pytest
from autonoma.agent import Agent
from autonoma.config import Settings
from autonoma.filesystem import FileSystemError, FileSystemManager
from autonoma.key_handler import PanicController, PanicError
from autonoma.knowledge import KnowledgeStore
from autonoma.notrack_client import NoTrackClient
from autonoma.search_engine import SearchEngine, SearchHit
from autonoma.tool_registry import ToolRegistry


@pytest.fixture
def fs():
    return FileSystemManager(PanicController())


def test_crud(fs, tmp_path):
    directory = tmp_path/'files'
    fs.mkdir(str(directory))
    source = directory/'a'
    fs.write_file(str(source), 'hello')
    fs.append_file(str(source), ' world')
    assert fs.read_file(str(source)) == 'hello world'
    assert 'truncado' in fs.read_file(str(source), max_chars=2)
    fs.copy_path(str(source), str(directory/'b'))
    fs.move_path(str(directory/'b'), str(directory/'c'))
    assert 'c' in fs.list_dir(str(directory))
    assert 'más' in fs.list_dir(str(directory), max_entries=1)
    fs.delete_path(str(directory/'c'))
    fs.copy_path(str(directory), str(tmp_path/'copy'))
    fs.delete_path(str(tmp_path/'copy'))
    assert not (tmp_path/'copy').exists()


def test_crud_invalid(fs, tmp_path):
    with pytest.raises(FileSystemError, match='No existe'):
        fs.read_file(str(tmp_path/'missing'))
    with pytest.raises(FileSystemError, match='directorio'):
        fs.read_file(str(tmp_path))
    file = tmp_path/'a'
    file.write_text('a')
    with pytest.raises(FileSystemError, match='directorio'):
        fs.list_dir(str(file))
    with pytest.raises(FileSystemError, match='existe'):
        fs.copy_path(str(file), str(file))
    with pytest.raises(FileSystemError, match='existe'):
        fs.move_path(str(file), str(file))
    with pytest.raises(FileSystemError, match='dentro'):
        fs.copy_path(str(tmp_path), str(tmp_path/'nested'))


def test_symlink_copy_rejected(fs, tmp_path):
    source = tmp_path/'source'
    source.mkdir()
    (source/'link').symlink_to(tmp_path/'private')
    with pytest.raises(FileSystemError, match='simbólicos'):
        fs.copy_path(str(source), str(tmp_path/'copy'))


def test_delete_ancestor_of_protected(tmp_path):
    fs = FileSystemManager(PanicController(), extra_protected=[str(tmp_path/'private')])
    with pytest.raises(FileSystemError, match='protegida'):
        fs.delete_path(str(tmp_path))
    assert tmp_path.exists()


def test_notes_and_registry(tmp_path):
    panic = PanicController()
    notes = KnowledgeStore(panic, tmp_path)
    notes.save_note('alpha', 'one')
    notes.save_note('beta', 'two')
    assert 'one' in notes.search_notes('one')
    assert 'Sin coincidencias' in notes.search_notes('missing')
    assert 'truncado' in notes.read_note('alpha', max_chars=2)
    assert notes.context_digest()
    engine = SearchEngine(panic, tmp_path)
    bundle_file = engine.save_findings('topic', [SearchHit('Title', 'https://example.com', 'snippet', extra='extra', content='body')], notes='note')
    assert 'body' in bundle_file.read_text(encoding='utf-8')
    registry = ToolRegistry(engine, FileSystemManager(panic))
    assert 'Guardado' in registry.execute('save_knowledge', {'title': 'gamma', 'content': 'three'})
    assert 'gamma' in registry.execute('list_knowledge', {})
    assert 'three' in registry.execute('read_knowledge', {'query': 'gamma'})
    assert 'Sin coincidencias' in registry.execute('read_knowledge', {'query': 'missing'})
    engine.close()


def test_orchestration_tool_cycle(tmp_path):
    panic = PanicController()
    engine = SearchEngine(panic, tmp_path)
    seen = []
    call = {'type': 'function', 'id': '1', 'function': {'name': 'save_knowledge', 'arguments': '{"title":"test","content":"body"}'}}
    responses = iter([{'choices': [{'message': {'content': None, 'tool_calls': [call]}}]}, {'choices': [{'message': {'content': 'done'}}]}])
    def chat(messages, **kwargs):
        seen.append(list(messages))
        return next(responses)
    client = NoTrackClient('test', panic)
    client.chat = chat
    agent = Agent(Settings(), panic, client, engine, FileSystemManager(panic))
    events = []
    assert agent.run('save', lambda *a: events.append(a)) == 'done'
    assert seen[1][-1]['role'] == 'tool'
    assert 'Guardado' in seen[1][-1]['content']
    assert any(kind == 'timing' for kind, _ in events)
    assert not panic.busy


def test_invalid_tool_error_and_limit(tmp_path):
    panic = PanicController()
    call = {'type': 'function', 'id': '1', 'function': {'name': 'read_file', 'arguments': '{bad'}}
    client = SimpleNamespace(chat=lambda *a, **k: {}, extract_message=lambda _: {'tool_calls': [call]})
    agent = Agent(Settings(max_tool_iterations=1), panic, client, SearchEngine(panic, tmp_path), None)
    events = []
    assert 'límite' in agent.run('test', lambda *a: events.append(a))
    assert any(kind == 'tool_result' and 'ERROR' in text for kind, text in events)
    assert not panic.busy


def test_cancel_and_cleanup():
    panic = PanicController()
    cleaned = []
    callback = lambda: cleaned.append(True)
    panic.register_cleanup(callback)
    panic.panic()
    assert cleaned == [True]
    panic.unregister_cleanup(callback)
    panic.unregister_cleanup(callback)
    panic.reset()
    panic.panic()
    assert cleaned == [True]
    with pytest.raises(PanicError):
        panic.check()


def test_destructive_symlink_does_not_delete_target(fs, tmp_path):
    target = tmp_path/'important'
    target.write_text('keep')
    link = tmp_path/'link'
    link.symlink_to(target)
    with pytest.raises(FileSystemError, match='simbólicos'):
        fs.delete_path(str(link))
    with pytest.raises(FileSystemError, match='simbólicos'):
        fs.write_file(str(link), 'replace')
    assert target.read_text() == 'keep'
