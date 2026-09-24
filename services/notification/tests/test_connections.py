"""7.4: ConnectionManager: several sockets per person, dead sockets dropped, nobody gets someone else's message."""
from app.connections import ConnectionManager
from conftest import NUSRAT, RAFIQ


class FakeSocket:
    def __init__(self, broken: bool = False):
        self.accepted, self.sent, self.broken = False, [], broken

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        if self.broken:
            raise RuntimeError("connection reset")
        self.sent.append(message)


async def test_connect_accepts_and_send_reaches_the_socket():
    m, phone = ConnectionManager(), FakeSocket()
    await m.connect(NUSRAT, phone)
    await m.send(NUSRAT, {"type": "ride.matched"})
    assert phone.accepted and phone.sent == [{"type": "ride.matched"}]


async def test_phone_and_tablet_both_get_it():
    m, phone, tablet = ConnectionManager(), FakeSocket(), FakeSocket()
    await m.connect(NUSRAT, phone)
    await m.connect(NUSRAT, tablet)
    await m.send(NUSRAT, {"n": 1})
    assert phone.sent == tablet.sent == [{"n": 1}]


async def test_nobody_gets_someone_elses_message():
    m, nusrat, rafiq = ConnectionManager(), FakeSocket(), FakeSocket()
    await m.connect(NUSRAT, nusrat)
    await m.connect(RAFIQ, rafiq)
    await m.send(NUSRAT, {"fare": 7200})
    assert nusrat.sent == [{"fare": 7200}] and rafiq.sent == []


async def test_a_dead_socket_is_dropped_and_the_others_still_get_it():
    m, dead, alive = ConnectionManager(), FakeSocket(broken=True), FakeSocket()
    await m.connect(NUSRAT, dead)
    await m.connect(NUSRAT, alive)
    await m.send(NUSRAT, {"n": 1})
    await m.send(NUSRAT, {"n": 2})
    assert alive.sent == [{"n": 1}, {"n": 2}]
    assert m._conns[NUSRAT] == {alive}


async def test_last_socket_gone_forgets_the_person():
    m, phone = ConnectionManager(), FakeSocket()
    await m.connect(NUSRAT, phone)
    m.disconnect(NUSRAT, phone)
    assert NUSRAT not in m._conns
    m.disconnect(NUSRAT, phone)  # twice is harmless (the socket loop and a failed send can both do it)


async def test_sending_to_someone_offline_is_a_no_op():
    m = ConnectionManager()
    await m.send(RAFIQ, {"n": 1})
    assert RAFIQ not in m._conns  # asking didn't create an empty entry
