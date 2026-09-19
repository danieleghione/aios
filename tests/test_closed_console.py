"""The physical console is a closed menu: no shell, no login on other terminals,
no editable boot entries."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def menu_commands(path):
    text = (ROOT / path).read_text()
    return [line for line in text.splitlines() if not line.lstrip().startswith('#')]


def test_installed_console_offers_no_shell():
    lines = menu_commands('installer/console.sh')
    assert not [line for line in lines if re.search(r'(^|[\s;)])(/bin/)?(ba)?sh(\s|;|$)', line)]
    assert not any('recovery shell' in line.lower() for line in lines)
    assert "trap '' INT QUIT TSTP" in lines


def test_installer_menu_offers_no_shell():
    lines = menu_commands('installer/live-console.sh')
    assert not [line for line in lines if re.search(r'(^|[\s;)])(/bin/)?(ba)?sh(\s|;|$)', line)]
    assert not any('shell' in line.lower() for line in lines if line.lstrip().startswith('echo'))


def test_installing_updates_needs_an_administrator():
    text = (ROOT / 'installer/console.sh').read_text()
    block = text[text.index('    3)\n'):text.index('    6)\n')]
    assert 'no_administrator updates' in block and 'authorize updates' in block


def test_installation_asks_no_erase_confirmation_and_locks_grub():
    text = (ROOT / 'installer/install.sh').read_text()
    assert 'ERASE' not in text
    assert 'password_pbkdf2 aios-locked' in text and '--unrestricted' in text


def test_image_disables_other_terminals_and_sysrq():
    text = (ROOT / 'scripts/build-image.sh').read_text()
    assert 'NAutoVTs=0' in text and 'kernel.sysrq=0' in text
    assert 'GRUB_DISABLE_RECOVERY=true' in text and 'splash' in text


def test_recovery_ssh_is_offered_enabled():
    text = (ROOT / 'installer/configure.py').read_text()
    assert "ask('Enable recovery SSH? yes/no', 'yes', yes_no)" in text


def test_open_webui_defaults_keep_the_cpu_free_between_answers():
    """Chat tasks and built-in tools used the local CPU model for minutes per message."""
    import json
    text = (ROOT / 'scripts/firstboot.sh').read_text()
    block = text[text.index('ENABLE_TITLE_GENERATION=true'):]
    block = block[:block.index('\nENV')]
    values = dict(line.split('=', 1) for line in block.splitlines())
    for task in ('TAGS', 'FOLLOW_UP', 'AUTOCOMPLETE', 'SEARCH_QUERY', 'RETRIEVAL_QUERY'):
        assert values[f'ENABLE_{task}_GENERATION'] == 'false'
    params = json.loads(values['TASK_MODEL_PARAMS'].strip("'"))
    assert params['chat_template_kwargs'] == {'enable_thinking': False} and params['reasoning_budget_tokens'] == 0
    assert json.loads(values['DEFAULT_MODEL_METADATA'].strip("'")) == {'capabilities': {'builtin_tools': False}}
    # Reconciled on every boot, not only when the file is first written.
    # The reconciliation line removes what it is about to rewrite, so a restored
    # installation cannot keep an older value of any of these.
    removal = next(line for line in text.splitlines() if line.startswith('sed -i -E') and 'webui.env' in line)
    assert all(key in removal for key in ('TASK_MODEL_PARAMS', 'DEFAULT_MODEL_METADATA', 'ENABLE_IMAGE_GENERATION'))
