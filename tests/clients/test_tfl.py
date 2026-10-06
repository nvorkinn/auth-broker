from unittest.mock import Mock

import pytest

from broker.clients.tfl import _get_stop_point


def test_get_stop_point_asks_tfl_for_the_stop_id(app):
    http = Mock()
    http.get.return_value = Mock(status_code=200, json=Mock(return_value={"id": "940GZZLUKSX"}))

    with app.app_context():
        assert _get_stop_point("940GZZLUKSX", http) == {"id": "940GZZLUKSX"}

    assert http.get.call_args.args == ("https://api.tfl.gov.uk/StopPoint/940GZZLUKSX",)


@pytest.mark.parametrize("stop_id", ["../Line", "a/b", "a?b=c", "a#b", "a b", "", "%2e%2e"])
def test_get_stop_point_rejects_ids_that_could_change_the_url_path(app, stop_id):
    http = Mock()

    with app.app_context():
        assert _get_stop_point(stop_id, http) is None

    http.get.assert_not_called()
