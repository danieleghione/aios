"""What the Hardware page lists."""

def test_each_disk_is_listed_once(environment, monkeypatch):
    from collections import namedtuple
    from aios import hardware
    part = namedtuple('part', 'device mountpoint fstype')
    usage = namedtuple('usage', 'total used free percent')
    parts = [part('/dev/sda2', '/tmp', 'ext4'), part('/dev/sda2', '/', 'ext4'), part('/dev/sda3', '/var/lib/aios', 'ext4'),
             part('/dev/sda2', '/var/log/aios', 'ext4'), part('/dev/sda3', '/var/lib/aios/runtime', 'ext4')]
    monkeypatch.setattr(hardware.psutil, 'disk_partitions', lambda: parts)
    monkeypatch.setattr(hardware.psutil, 'disk_usage', lambda path: usage(10, 5, 5, 50.0))
    disks = hardware.profile(probe=False)['disks']
    assert [(d['device'], d['mount']) for d in disks] == [('/dev/sda2', '/'), ('/dev/sda3', '/var/lib/aios')]
