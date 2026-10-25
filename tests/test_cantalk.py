"""Run with PYTHONPATH=src python -m unittest discover -s tests.

Set CANTALK_TEST_IFACE to a dedicated vcan interface for kernel integration tests.
"""
import contextlib
import os
import pty
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from canconf import cantalk as t


class FunctionalTests(unittest.TestCase):
    def setUp(self):
        self.state = t.State("vcan0", False, 0.8, 0xAA)
        self.addCleanup(self.state.close_socket)

    def test_address_derivation(self):
        for tx in (0x7DF, 0x18DB33F1, 0x18DB33FA):
            with self.subTest(tx=tx):
                self.assertIsNone(t.derive_rx(tx))
        self.assertEqual(t.derive_rx(0x7E0), 0x7E8)
        self.assertEqual(t.derive_rx(0x18DA10F1), 0x18DAF110)
        self.assertEqual(t.functional_pairs(0x7DF),
                         [(0x7E0 + i, 0x7E8 + i) for i in range(8)])
        pairs = t.functional_pairs(0x18DB33FA)
        self.assertEqual(len(pairs), 255)
        self.assertIn((0x18DA10FA, 0x18DAFA10), pairs)
        self.assertIn((0x18DA00FA, 0x18DAFA00), pairs)
        self.assertIn((0x18DAFFFA, 0x18DAFAFF), pairs)
        self.assertNotIn((0x18DAFAFA, 0x18DAFAFA), pairs)

    def test_command_labels_and_rebinding(self):
        sockets = []

        def new_socket(*args, **kwargs):
            sock = Mock()
            sockets.append(sock)
            return sock

        with patch.object(t, "open_isotp", side_effect=new_socket), \
                patch.object(t, "open_raw", side_effect=new_socket), \
                patch.object(t, "log") as log:
            t.handle_command(self.state, "7DF")
            self.assertEqual(self.state.rx_label(), "7E8-7EF")
            self.assertEqual(len(self.state.responders), 8)
            self.assertIn("7E8-7EF", self.state.simple_prompt())
            self.assertTrue(any("rx auto-derived: 7E8-7EF" in str(call)
                                for call in log.call_args_list))
            old_sockets = sockets[:]
            t.handle_command(self.state, "18DB33F1")
            self.assertEqual(self.state.rx_label(), "18DAF1xx")
            self.assertEqual(len(self.state.responders), 255)
            for sock in old_sockets:
                sock.close.assert_called_once()
            t.handle_command(self.state, "7E0")
            self.assertEqual(self.state.rx_id, 0x7E8)
            self.assertFalse(self.state.responders)

    def test_explicit_rx_and_raw_keep_single_socket(self):
        with patch.object(t, "open_isotp") as isotp, patch.object(t, "open_raw") as raw:
            self.state.set_arbitration(0x7DF, 0x7E9)
            isotp.assert_called_once_with("vcan0", 0x7DF, 0x7E9, 0xAA)
            self.assertEqual(self.state.rx_label(), "7E9")
            self.assertFalse(self.state.responders)
            self.state.raw = True
            self.state.set_arbitration(0x7DF, None)
            raw.assert_called_once_with("vcan0", None)
            self.assertEqual(self.state.rx_label(), "*")

    def test_partial_open_failure_cleans_up(self):
        first = Mock()
        with patch.object(t, "open_isotp", side_effect=[first, OSError("bind failed")]):
            with self.assertRaises(OSError):
                self.state.set_arbitration(0x7DF, None)
        first.close.assert_called_once()
        self.assertFalse(self.state.responders)
        self.assertIsNone(self.state.sock)
        self.assertIsNone(self.state.tx_id)

    def test_isotp_bind_extended_ids_and_failure_cleanup(self):
        sock = Mock()
        with patch.object(t.socket, "socket", return_value=sock):
            t.open_isotp("vcan0", 0x18DA10F1, 0x18DAF110, 0xAA)
            sock.bind.assert_called_once_with(("vcan0", 0x98DAF110, 0x98DA10F1))
            sock.bind.side_effect = OSError("bind failed")
            with self.assertRaises(OSError):
                t.open_isotp("vcan0", 0x7E0, 0x7E8, None)
            sock.close.assert_called_once()

    def test_functional_request_is_single_frame_and_length_checked(self):
        self.state.tx_id = 0x7DF
        self.state.sock = Mock()
        self.state.responders = {Mock(): 0x7E8}
        with patch.object(t, "log"), \
                patch.object(t, "receive_pending"):
            for padding in (0xAA, None):
                self.state.padding = padding
                t.send_payload(self.state, b"\x09\x02")
                frame = self.state.sock.send.call_args.args[0]
                can_id, dlc, data = t.struct.unpack(t.CAN_FRAME_FMT, frame)
                self.assertEqual(can_id, 0x7DF)
                self.assertEqual(dlc, 8 if padding is not None else 3)
                self.assertEqual(data[:3], b"\x02\x09\x02")
                if padding is not None:
                    self.assertEqual(data[3:], b"\xAA" * 5)
            self.state.sock.send.reset_mock()
            t.send_payload(self.state, bytes(7))
            self.state.sock.send.assert_called_once()
            self.state.sock.send.reset_mock()
            for payload in (b"", bytes(8)):
                t.send_payload(self.state, payload)
            self.state.sock.send.assert_not_called()

    def test_saved_functional_arbitration_restores_all_receivers(self):
        with patch.object(t, "open_isotp", side_effect=lambda *args: Mock()), \
                patch.object(t, "open_raw"):
            self.assertTrue(t._restore_arbitration(self.state, {"tx": "7DF", "rx": None}))
        self.assertEqual(self.state.rx_label(), "7E8-7EF")
        self.assertEqual(len(self.state.responders), 8)

    def test_receive_error_does_not_hide_other_ecus(self):
        broken, working = Mock(), Mock()
        broken.recv.side_effect = OSError("bad sequence")
        working.recv.return_value = b"\x41\x00"
        self.state.responders = {broken: 0x7E8, working: 0x7E9}
        with patch.object(t, "log"), patch.object(t, "print_response") as response:
            t.receive_one(self.state, broken, 0x7E8)
            t.receive_one(self.state, working, 0x7E9)
        response.assert_called_once_with(0x7E9, b"\x41\x00")


class ReceiveStreamTests(unittest.TestCase):
    def setUp(self):
        self.client, self.ecu = socket.socketpair(type=socket.SOCK_DGRAM)
        self.ecu.settimeout(1)
        self.state = t.State("test", False, 0.05, None)
        self.state.sock = self.client
        self.state.tx_id, self.state.rx_id = 0x18DA00F9, 0x18DAF900
        self.addCleanup(self.state.close_socket)
        self.addCleanup(self.ecu.close)

    def test_queued_message_is_shown_before_new_send(self):
        self.ecu.send(bytes.fromhex("5A8000003169"))
        events = []

        def reply():
            self.ecu.recv(4096)
            self.ecu.send(b"\x7E")

        worker = threading.Thread(target=reply, daemon=True)
        worker.start()
        with patch.object(t, "print_request", side_effect=lambda _, b: events.append(("tx", b))), \
                patch.object(t, "print_response", side_effect=lambda _, b: events.append(("rx", b))):
            t.send_payload(self.state, b"\x3E")
            t.Term(self.state, [], active=False)._wait_for_input(finish=True)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(events, [("rx", bytes.fromhex("5A8000003169")),
                                  ("tx", b"\x3E"), ("rx", b"\x7E")])
        self.client.setblocking(False)
        with self.assertRaises(BlockingIOError):
            self.client.recv(4096)

    def test_every_message_is_displayed_without_service_filtering(self):
        messages = [b"\x7F\x3E\x78", b"\x5A\x80", b"\x7E", b"\x01\x02"]
        for message in messages:
            self.ecu.send(message)
        with patch.object(t, "print_response") as response:
            t.receive_pending(self.state)
        self.assertEqual([call.args[1] for call in response.call_args_list], messages)
        self.assertIsNone(self.client.gettimeout())

    def test_timeout_notice_does_not_require_a_keypress(self):
        with patch.object(t, "log") as log:
            t.send_payload(self.state, b"\x3E")
            term = t.Term(self.state, [], active=False)
            term._wait_for_input(finish=True)
            term._wait_for_input(finish=True)
        notices = [call for call in log.call_args_list if "no incoming message" in str(call)]
        self.assertEqual(len(notices), 1)
        self.assertIsNone(self.state.receive_until)

    def test_timeout_does_not_disable_late_reception(self):
        with patch.object(t, "log"):
            t.send_payload(self.state, b"\x3E")
            t.Term(self.state, [], active=False)._wait_for_input(finish=True)
        self.ecu.send(b"\x7E")
        with patch.object(t, "print_response") as response:
            t.receive_pending(self.state)
        response.assert_called_once_with(0x18DAF900, b"\x7E")

    def test_receives_while_waiting_for_keyboard_in_plain_and_tui_modes(self):
        for tui in (False, True):
            with self.subTest(tui=tui):
                read_fd, write_fd = os.pipe()
                displayed = threading.Event()
                term = t.Term(self.state, [], active=False)
                term.is_tty, term.fd = tui, read_fd

                def deliver():
                    self.ecu.send(b"\x5A\x80")
                    # Keyboard input arrives only after the response was shown.
                    displayed.wait(1)
                    os.write(write_fd, b"\n")

                worker = threading.Thread(target=deliver, daemon=True)
                worker.start()
                try:
                    with patch.object(t.sys, "stdin") as stdin, \
                            patch.object(t, "print_response", side_effect=lambda *_: displayed.set()) as response:
                        stdin.fileno.return_value = read_fd
                        term._wait_for_input()
                    self.assertTrue(displayed.is_set())
                    response.assert_called_once_with(0x18DAF900, b"\x5A\x80")
                finally:
                    worker.join(2)
                    os.close(read_fd)
                    os.close(write_fd)


class TerminalResponsivenessTests(unittest.TestCase):
    def test_piped_commands_do_not_wait_and_eof_keeps_receiving(self):
        client, ecu = socket.socketpair(type=socket.SOCK_DGRAM)
        code = '''
import socket, sys
from canconf import cantalk as t
state = t.State('test', False, .2, None)
state.sock = socket.socket(fileno=int(sys.argv[1]))
state.tx_id, state.rx_id = 0x7E0, 0x7E8
with t.Term(state, [], active=False) as term:
    t._active_term = term
    t.repl(state, term)
state.close_socket()
'''
        with contextlib.ExitStack() as stack:
            stack.callback(client.close)
            stack.callback(ecu.close)
            process = subprocess.Popen([sys.executable, "-c", code, str(client.fileno())],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, pass_fds=(client.fileno(),))
            stack.callback(process.wait)
            stack.callback(process.kill)
            stack.callback(process.stdout.close)
            stack.callback(process.stderr.close)
            stack.callback(process.stdin.close)
            process.stdin.write(b"3e\n1a80\n")
            process.stdin.flush()
            ecu.settimeout(0.75)
            self.assertEqual(ecu.recv(4096), b"\x3E")
            self.assertEqual(ecu.recv(4096), b"\x1A\x80")
            process.stdin.close()
            ecu.send(b"\x7E")
            ecu.send(b"\x5A\x80")
            self.assertEqual(process.wait(timeout=1), 0)
            output = process.stdout.read()
            self.assertIn(b"7E", output)
            self.assertIn(b"5A 80", output)

    def test_typing_and_reception_immediately_after_send_in_all_modes(self):
        code = '''
import socket, sys
from canconf import cantalk as t
mode = sys.argv[1]
state = t.State('test', mode == 'raw', 2, None)
state.sock = socket.socket(fileno=int(sys.argv[2]))
state.tx_id, state.rx_id = 0x7E0, 0x7E8
if mode == 'functional':
    state.tx_id, state.rx_id = 0x7DF, None
    state.responders = {socket.socket(fileno=int(sys.argv[3])): 0x7E8}
with t.Term(state, []) as term:
    t._active_term = term
    t.repl(state, term)
t._active_term = None
state.close_socket()
'''
        for mode in ("isotp", "raw", "functional"):
            with self.subTest(mode=mode), contextlib.ExitStack() as stack:
                sender, ecu = socket.socketpair(type=socket.SOCK_DGRAM)
                receiver, reply = socket.socketpair(type=socket.SOCK_DGRAM)
                for sock in (sender, ecu, receiver, reply):
                    stack.callback(sock.close)
                master, slave = pty.openpty()
                stack.callback(os.close, master)
                stack.callback(os.close, slave)
                process = subprocess.Popen(
                    [sys.executable, "-c", code, mode, str(sender.fileno()), str(receiver.fileno())],
                    stdin=slave, stdout=slave, stderr=slave,
                    pass_fds=(sender.fileno(), receiver.fileno()),
                    env=dict(os.environ, TERM="xterm"))
                stack.callback(process.wait)
                stack.callback(process.kill)

                def await_output(needle, timeout=1):
                    output = b""
                    deadline = time.monotonic() + timeout
                    while needle not in output:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not t.select.select([master], [], [], remaining)[0]:
                            self.fail(f"Missing {needle!r} in terminal output: {output!r}")
                        output += os.read(master, 65536)
                    return output

                await_output("❯".encode())
                os.write(master, b"3e\n")
                ecu.settimeout(0.75)
                request = ecu.recv(4096)
                self.assertEqual(request if mode == "isotp" else
                                 t.struct.unpack(t.CAN_FRAME_FMT, request)[2][:2],
                                 b"\x3E" if mode == "isotp" else
                                 b"\x01\x3E" if mode == "functional" else b"\x3E\x00")
                os.write(master, b"0902")
                await_output(b"0902", timeout=0.75)  # Must not wait the two-second timeout.
                if mode == "raw":
                    ecu.send(t.struct.pack(t.CAN_FRAME_FMT, 0x7E8, 1, b"\x7E"))
                else:
                    (reply if mode == "functional" else ecu).send(b"\x7E")
                await_output(b"7E", timeout=0.75)
                os.write(master, b"\n")
                request = ecu.recv(4096)
                self.assertEqual(request if mode == "isotp" else
                                 t.struct.unpack(t.CAN_FRAME_FMT, request)[2][:3],
                                 b"\x09\x02" if mode == "isotp" else
                                 b"\x02\x09\x02" if mode == "functional" else b"\x09\x02\x00")
                os.write(master, b"\x04")
                self.assertEqual(process.wait(timeout=0.75), 0)


@unittest.skipUnless(os.environ.get("CANTALK_TEST_IFACE"), "needs dedicated vcan interface")
class KernelFunctionalTests(unittest.TestCase):
    def test_queued_identification_then_tester_present(self):
        iface = os.environ["CANTALK_TEST_IFACE"]
        events = []
        errors = []
        with contextlib.ExitStack() as stack:
            state = t.State(iface, False, 0.15, 0xAA)
            stack.callback(state.close_socket)
            state.set_arbitration(0x18DA00F9, 0x18DAF900)
            ecu = stack.enter_context(contextlib.closing(
                t.open_isotp(iface, 0x18DAF900, 0x18DA00F9, 0xAA)))
            ecu.settimeout(1)
            old = bytes.fromhex("5A8000003169") + bytes(range(30))
            ecu.send(old)
            self.assertTrue(t.select.select([state.sock], [], [], 1)[0])
            monitor = stack.enter_context(contextlib.closing(t.open_raw(iface, 0x18DAF900)))
            monitor.settimeout(1)

            def reply():
                try:
                    self.assertEqual(ecu.recv(4096), b"\x3E")
                    ecu.send(b"\x7E")
                except BaseException as e:
                    errors.append(e)

            worker = threading.Thread(target=reply, daemon=True)
            worker.start()
            with patch.object(t, "print_request", side_effect=lambda _, b: events.append(("tx", b))), \
                    patch.object(t, "print_response", side_effect=lambda _, b: events.append(("rx", b))):
                t.send_payload(state, b"\x3E")
                t.Term(state, [], active=False)._wait_for_input(finish=True)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            if errors:
                raise errors[0]
            self.assertEqual(events, [("rx", old), ("tx", b"\x3E"), ("rx", b"\x7E")])
            self.assertEqual(t.recv_raw_frame(monitor)[1][:2], b"\x01\x7E")

    def exchange(self, tx, pairs, padding):
        iface = os.environ["CANTALK_TEST_IFACE"]
        replies = [bytes([0x49, i]) + bytes(range(30)) for i in range(len(pairs))]
        errors = []
        with contextlib.ExitStack() as stack:
            state = t.State(iface, False, 0.8, padding)
            stack.callback(state.close_socket)
            state.set_arbitration(tx, None)
            request_sock = stack.enter_context(contextlib.closing(t.open_raw(iface, tx)))
            request_sock.settimeout(2)
            ecus = []
            for physical_tx, rx in pairs:
                ecu = stack.enter_context(contextlib.closing(
                    t.open_isotp(iface, rx, physical_tx, padding)))
                ecu.settimeout(2)
                ecus.append(ecu)

            def respond():
                try:
                    can_id, request = t.recv_raw_frame(request_sock)
                    self.assertEqual(can_id, tx)
                    self.assertEqual(request[:3], b"\x02\x09\x02")
                    self.assertEqual(len(request), 8 if padding is not None else 3)
                    ecus[0].send(replies[0])
                    # Longer than the raw-mode quiet period: a second ECU must
                    # still be collected for the full configured timeout.
                    time.sleep(0.35)
                    ecus[1].send(replies[1])
                except BaseException as e:
                    errors.append(e)

            worker = threading.Thread(target=respond, daemon=True)
            worker.start()
            with patch.object(t, "log"), patch.object(t, "print_response") as response:
                t.send_payload(state, b"\x09\x02")
                t.Term(state, [], active=False)._wait_for_input(finish=True)
            worker.join(3)
            self.assertFalse(worker.is_alive(), "ECU thread did not finish")
            if errors:
                raise errors[0]
            self.assertEqual(response.call_args_list,
                             [unittest.mock.call(rx, data)
                              for (_, rx), data in zip(pairs, replies)])
            # Exactly one functional request was sent.
            request_sock.settimeout(0.01)
            with self.assertRaises(socket.timeout):
                request_sock.recv(t.CAN_FRAME_SIZE)

    def test_multiple_multiframe_11bit_replies(self):
        for padding in (0xAA, None):
            with self.subTest(padding=padding):
                self.exchange(0x7DF, [(0x7E0, 0x7E8), (0x7E7, 0x7EF)], padding)

    def test_multiple_multiframe_29bit_replies(self):
        self.exchange(0x18DB33F1,
                      [(0x18DA10F1, 0x18DAF110), (0x18DA20F1, 0x18DAF120)], 0xAA)


if __name__ == "__main__":
    unittest.main()
