from broker.db import db
from broker.models import Device


def test_verify_sends_an_unpaired_browser_to_pair(client):
    response = client.get("/api/logging_ui/verify")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/pair")


def test_verify_accepts_a_paired_browser(paired_client):
    client, _, _ = paired_client

    response = client.get("/api/logging_ui/verify")

    assert response.status_code == 200
    assert response.data == b""


def test_verify_rejects_a_browser_whose_device_was_forgotten(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.delete(db.session.get(Device, device_id))
        db.session.commit()

    response = client.get("/api/logging_ui/verify")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/pair")


def test_verify_rejects_a_browser_whose_device_was_unpaired(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(Device, device_id).paired_at = None
        db.session.commit()

    response = client.get("/api/logging_ui/verify")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/pair")
