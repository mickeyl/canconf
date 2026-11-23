"""Run with PYTHONPATH=src python3 -m unittest discover -s tests."""
import unittest

from canconf import topology as t


def result(ifaces, heard, errors=None):
    r = t.ProbeResult(ifaces)
    for src, dsts in heard.items():
        r.heard[src] |= set(dsts)
    r.errors.update(errors or {})
    return r


class GroupingTests(unittest.TestCase):
    def test_buses(self):
        r = result(["can0", "can1", "can2", "can3", "can4"],
                   {"can0": ["can1", "can2"], "can1": ["can0", "can2"],
                    "can2": ["can0", "can1"], "can3": ["can4"], "can4": ["can3"]})
        self.assertEqual(t.group_buses(r),
                         [["can0", "can1", "can2"], ["can3", "can4"]])

    def test_one_way_still_joins_and_warns(self):
        r = result(["a", "b"], {"a": ["b"]})
        self.assertEqual(t.group_buses(r), [["a", "b"]])
        self.assertEqual(t.asymmetric_pairs(r), [("a", "b")])

    def test_lone_nodes_sorted_last(self):
        r = result(["a", "b", "c"], {"b": ["c"], "c": ["b"]})
        self.assertEqual(t.group_buses(r), [["b", "c"], ["a"]])

    def test_failed_iface_not_counted_as_bus(self):
        r = result(["a", "b"], {}, errors={"b": "boom"})
        self.assertEqual(t.group_buses(r), [["a"]])

    def test_usable_modes(self):
        link = lambda m: {"flags": ["UP"], "linkinfo": {"info_data": {"ctrlmode": m}}}
        self.assertIn("listen-only", t.usable(link(["LISTEN-ONLY"])))
        self.assertIn("loopback", t.usable(link(["LOOPBACK"])))
        self.assertIsNone(t.usable(link([])))

    def test_usable(self):
        up = {"flags": ["UP"], "linkinfo": {"info_data": {"state": "ERROR-ACTIVE"}}}
        self.assertIsNone(t.usable(up))
        self.assertEqual(t.usable(None), "missing")
        self.assertEqual(t.usable({"flags": []}), "down")
        off = {"flags": ["UP"], "linkinfo": {"info_data": {"state": "BUS-OFF"}}}
        self.assertEqual(t.usable(off), "bus-off")


class DiagnoseTests(unittest.TestCase):
    @staticmethod
    def link(state, rx, tx, passive=0):
        return {"stats64": {"rx": {"packets": rx}, "tx": {"packets": tx, "errors": 0}},
                "linkinfo": {"info_data": {"state": state},
                             "info_xstats": {"error_passive": passive}}}

    def test_error_state_reported(self):
        msg = t.diagnose(self.link("ERROR-ACTIVE", 0, 0),
                         self.link("ERROR-PASSIVE", 2, 2, passive=1))
        self.assertIn("ERROR-PASSIVE", msg)
        self.assertIn("not being ACKed", msg)

    def test_clean_lone_node(self):
        msg = t.diagnose(self.link("ERROR-ACTIVE", 0, 0), self.link("ERROR-ACTIVE", 2, 2))
        self.assertIn("no errors", msg)


class DriverTests(unittest.TestCase):
    def test_fallbacks(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/can9/device")
            os.symlink("/x/bus/usb/drivers/slcan_drv", f"{d}/can9/device/driver")
            self.assertEqual(t.driver_label("can9", {}, d), "slcan_drv")
            self.assertEqual(t.driver_label("vcan0", {"linkinfo": {"info_kind": "vcan"}}, d), "vcan")
            const = {"linkinfo": {"info_data": {"bittiming_const": {"name": "gs_usb"}}}}
            self.assertEqual(t.driver_label("can9", const, d), "gs_usb")
            self.assertEqual(t.driver_label("none", None, d), "")


class RenderTests(unittest.TestCase):
    def test_render_contains_all_nodes(self):
        links = {n: {"linkinfo": {"info_data": {"bittiming": {"bitrate": 500000},
                                                 "bittiming_const": {"name": "gs_usb"}}}}
                 for n in ("can0", "can1", "can2")}
        r = result(["can0", "can1", "can2"], {"can0": ["can1"], "can1": ["can0"]})
        text = "\n".join(t.render(r, links, {"can9": "down"}))
        for needle in ("Bus 1", "Bus 2", "can0", "can1", "can2", "500k", "gs_usb",
                       "can9: not probed (down)", "3 interfaces probed, 2 buses"):
            self.assertIn(needle, text)

    def test_box_columns_align(self):
        lines = t.render_bus("Bus", [("can0", "500k"), ("can10", "")])
        bus, stem, top = lines[1], lines[2], lines[3]
        for i, ch in enumerate(stem):
            if ch == "│":
                self.assertEqual(bus[i], "╤")
                self.assertEqual(top[i], "┴")


if __name__ == "__main__":
    unittest.main()
