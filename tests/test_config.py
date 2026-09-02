"""Settings values that are more than a plain read of the environment."""

from tests.fakes import fake_settings


def test_a_copy_list_is_split_on_commas() -> None:
    settings = fake_settings(UAE_CC="a@x.invalid,b@x.invalid")
    assert settings.region_cc["uae"] == ["a@x.invalid", "b@x.invalid"]


def test_spaces_and_a_trailing_comma_are_tolerated() -> None:
    """People edit .env by hand, and " a@x, b@x," is what that looks like."""
    settings = fake_settings(SG_CC=" a@x.invalid ,  b@x.invalid , ")
    assert settings.region_cc["sg"] == ["a@x.invalid", "b@x.invalid"]


def test_an_unset_copy_list_is_empty_not_a_blank_address() -> None:
    """A [""] would put an empty recipient into the Graph payload."""
    assert fake_settings().region_cc == {"uae": [], "sg": []}


def test_a_single_address_needs_no_comma() -> None:
    assert fake_settings(UAE_CC="only@x.invalid").region_cc["uae"] == ["only@x.invalid"]


def test_the_desk_addresses_stay_plain_strings() -> None:
    settings = fake_settings(UAE_MAILBOX="desk@x.invalid")
    assert settings.region_mailboxes == {"uae": "desk@x.invalid", "sg": ""}
