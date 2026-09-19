"""A refused model must explain itself without sending the operator to a log file."""
from aios.runtime import load_failure

REAL_LOG = """0.00.062.552 W srv  llama_server: -----------------
0.00.085.960 I srv    load_model: loading model '/var/lib/aios/models/c6b9aae7.gguf'
0.00.261.954 E llama_model_load: error loading model: error loading model hyperparameters: key not found in model: qwen4exp.context_length
0.00.261.963 E llama_model_load_from_file_impl: failed to load model
0.00.285.139 I srv    operator(): cleaning up before exit...
0.00.285.696 E srv  llama_server: exiting due to model loading error
"""


def test_reports_the_cause_not_the_exit_notice(tmp_path):
    log = tmp_path / 'runtime.log'
    log.write_text(REAL_LOG)
    message = load_failure(log)
    assert 'qwen4exp.context_length' in message
    assert 'not supported by the bundled llama.cpp build' in message
    # The generic "exiting due to..." line must not displace the real reason.
    assert 'exiting due to model loading error' not in message


def test_falls_back_when_the_log_says_nothing(tmp_path):
    log = tmp_path / 'runtime.log'
    log.write_text('0.00.001 I srv starting\n')
    assert load_failure(log) == 'llama-server exited during load; consult runtime log'


def test_missing_log_does_not_raise(tmp_path):
    assert load_failure(tmp_path / 'absent.log') == 'llama-server exited during load; consult runtime log'


def test_message_stays_bounded(tmp_path):
    log = tmp_path / 'runtime.log'
    log.write_text('0.00.001 E llama_model_load: error loading model: ' + 'x' * 5000 + '\n')
    assert len(load_failure(log)) < 500
