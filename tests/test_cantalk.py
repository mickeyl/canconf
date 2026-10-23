"""Run with PYTHONPATH=src python -m unittest discover -s tests.

Set CANTALK_TEST_IFACE to a dedicated vcan interface for kernel integration tests.
"""
import contextlib
import os
import socket
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
        with patch.object(t, "collect_functional"), patch.object(t, "log"):
            for padding in (0xAA, None):
                self.state.padding = padding
                t.send_and_receive(self.state, b"\x09\x02")
                frame = self.state.sock.send.call_args.args[0]
                can_id, dlc, data = t.struct.unpack(t.CAN_FRAME_FMT, frame)
                self.assertEqual(can_id, 0x7DF)
                self.assertEqual(dlc, 8 if padding is not None else 3)
                self.assertEqual(data[:3], b"\x02\x09\x02")
                if padding is not None:
                    self.assertEqual(data[3:], b"\xAA" * 5)
            self.state.sock.send.reset_mock()
            t.send_and_receive(self.state, bytes(7))
            self.state.sock.send.assert_called_once()
            self.state.sock.send.reset_mock()
            for payload in (b"", bytes(8)):
                t.send_and_receive(self.state, payload)
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
        selector = Mock()
        selector.select.side_effect = [
            [(Mock(fileobj=broken, data=0x7E8), 1),
             (Mock(fileobj=working, data=0x7E9), 1)], []]
        with patch.object(t.selectors, "DefaultSelector") as factory, \
                patch.object(t, "log"), patch.object(t, "print_response") as response:
            factory.return_value.__enter__.return_value = selector
            t.collect_functional(self.state)
        response.assert_called_once_with(0x7E9, b"\x41\x00")
        selector.unregister.assert_called_once_with(broken)

    def test_no_response_reports_timeout(self):
        self.state.responders = {Mock(): 0x7E8}
        with patch.object(t.selectors, "DefaultSelector") as factory, \
                patch.object(t, "log") as log:
            factory.return_value.__enter__.return_value.select.return_value = []
            t.collect_functional(self.state)
        self.assertIn("no response within 0.8s", log.call_args.args[0])


@unittest.skipUnless(os.environ.get("CANTALK_TEST_IFACE"), "needs dedicated vcan interface")
class KernelFunctionalTests(unittest.TestCase):
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
                t.send_and_receive(state, b"\x09\x02")
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
