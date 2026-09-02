"""Which regional desk an RFQ is routed to.

Pure rules over the shipped table, no model and no network. Getting this wrong
sends a real customer's RFQ to a desk that cannot fulfil it, so every rule and
every way of failing to match has a case here.
"""

from pathlib import Path

import pytest

from src.domain.rules.regions import Region, resolve_region
from src.domain.rules.registries import Registries

REGISTRIES_PATH = Path("config/registries.yaml")
DESKS = {"uae": "uae-desk@example.invalid", "sg": "sg-desk@example.invalid"}
REGIONS = Registries.load(REGISTRIES_PATH, region_mailboxes=DESKS).regions


# --------------------------------------------------------------------------- #
# The shipped table, and where the addresses come from
# --------------------------------------------------------------------------- #


def test_both_desks_are_recognisable() -> None:
    assert set(REGIONS) == {"uae", "sg"}


def test_the_committed_file_holds_no_address() -> None:
    """These are real mailboxes; the recipients belong in .env, not in git."""
    shipped = Registries.load(REGISTRIES_PATH).regions
    assert all(not region.forward_to for region in shipped.values())


def test_the_addresses_arrive_from_the_environment() -> None:
    assert REGIONS["uae"].forward_to == "uae-desk@example.invalid"
    assert REGIONS["sg"].forward_to == "sg-desk@example.invalid"


def test_a_desk_with_no_address_configured_stays_recognisable() -> None:
    """It still matches, so an ambiguous email is still seen as ambiguous.

    Refusing to forward is the handler's job; dropping the region here would
    quietly turn a conflict into a confident answer.
    """
    partial = Registries.load(REGISTRIES_PATH, region_mailboxes={"uae": "a@b.com"}).regions

    assert resolve_region(partial, text="eta Singapore").key == "sg"
    assert partial["sg"].forward_to == ""


def test_an_address_for_an_unknown_region_is_ignored() -> None:
    """A typo in .env must not invent a desk nothing can ever route to."""
    regions = Registries.load(REGISTRIES_PATH, region_mailboxes={"usa": "a@b.com"}).regions
    assert set(regions) == {"uae", "sg"}


# --------------------------------------------------------------------------- #
# The resolution chain, most trustworthy first
# --------------------------------------------------------------------------- #


def test_an_explicit_hint_wins() -> None:
    match = resolve_region(REGIONS, region_hint="UAE", text="delivery in Singapore")
    assert (match.key, match.rule) == ("uae", "region_hint")


def test_the_delivery_port_beats_a_keyword() -> None:
    """Corpus 006 reads "Fujairah"; a signature may still mention anything."""
    match = resolve_region(REGIONS, delivery_port="Fujairah", text="our Singapore office")
    assert (match.key, match.rule) == ("uae", "delivery_port")


def test_the_portal_writes_the_port_as_region_and_city() -> None:
    """Corpus 007 extracts "UAE - Dubai", not a bare port name."""
    match = resolve_region(REGIONS, delivery_port="UAE - Dubai")
    assert match.key == "uae"


PORTS = [
    ("Fujairah", "uae"),
    ("Port of Jebel Ali", "uae"),
    ("Mina Rashid", "uae"),
    ("Al Hamriya", "uae"),
    ("Port Khalid", "uae"),
    ("Khalifa Port", "uae"),
    ("Mina Zayed", "uae"),
    ("Port of Singapore", "sg"),
    ("Tuas Terminal", "sg"),
    ("Tanjong Pagar", "sg"),
    ("Keppel Terminal", "sg"),
    ("Brani Terminal", "sg"),
    ("Pasir Panjang Terminal", "sg"),
    ("Sembawang", "sg"),
    ("Jurong Port", "sg"),
]


@pytest.mark.parametrize(("port", "expected"), PORTS, ids=[p for p, _ in PORTS])
def test_every_configured_port_reaches_its_desk(port: str, expected: str) -> None:
    """One case per port, so a typo in the table fails here and not in production."""
    match = resolve_region(REGIONS, delivery_port=port)

    assert match is not None, f"{port} matched no region"
    assert match.key == expected


TERMINAL_COMPANY_NAMES = ["keppel", "jurong", "sembawang", "brani"]


@pytest.mark.parametrize("name", TERMINAL_COMPANY_NAMES)
def test_a_terminal_that_is_also_a_company_routes_only_as_a_port(name: str) -> None:
    """Keppel and Jurong are Singapore terminals and Singapore companies.

    As a delivery port the name is unambiguous. In prose it is just as likely to
    be a supplier's letterhead on an email about Dubai, so it stays out of the
    keywords and only the port rule may use it.
    """
    assert resolve_region(REGIONS, delivery_port=name).key == "sg"
    assert resolve_region(REGIONS, text=f"regards, {name} Offshore Marine") is None


def test_no_port_or_keyword_belongs_to_both_desks() -> None:
    """One shared entry would make every email that mentions it ambiguous."""
    for field in ("ports", "keywords", "mailbox_markers"):
        uae = set(getattr(REGIONS["uae"], field))
        sg = set(getattr(REGIONS["sg"], field))
        assert not uae & sg, f"{field} shared: {uae & sg}"


def test_a_keyword_in_the_text_is_enough() -> None:
    match = resolve_region(REGIONS, text="m/t North Star - eta Singapore next Tuesday")
    assert (match.key, match.rule) == ("sg", "keyword")


def test_the_mailbox_is_the_last_resort() -> None:
    match = resolve_region(REGIONS, mailbox="supply.uae@our-company.com")
    assert (match.key, match.rule) == ("uae", "mailbox")


def test_the_text_overrides_the_mailbox_it_arrived_in() -> None:
    """A Singapore delivery handled by the UAE desk still belongs to Singapore."""
    match = resolve_region(
        REGIONS, text="please quote for delivery Singapore", mailbox="supply.uae@our-company.com"
    )
    assert match.key == "sg"


# --------------------------------------------------------------------------- #
# Refusing to guess
# --------------------------------------------------------------------------- #


def test_two_regions_at_once_is_not_a_match() -> None:
    """Better unrouted and flagged than delivered to the wrong desk."""
    assert resolve_region(REGIONS, text="Dubai stock, Singapore delivery") is None


def test_an_ambiguous_rule_does_not_fall_through_to_a_weaker_one() -> None:
    """The mailbox must not settle a conflict the text could not."""
    assert (
        resolve_region(
            REGIONS,
            text="Dubai stock, Singapore delivery",
            mailbox="supply.uae@our-company.com",
        )
        is None
    )


def test_nothing_recognisable_is_not_a_match() -> None:
    assert resolve_region(REGIONS, text="please send your best price", mailbox="a@b.com") is None


def test_an_unknown_hint_does_not_route_anywhere() -> None:
    """A caller typing "middle east" must not silently land on a desk."""
    assert resolve_region(REGIONS, region_hint="middle east") is None


def test_a_short_keyword_does_not_fire_inside_a_longer_word() -> None:
    """"sg" sits inside "message" - substring matching would route half the corpus."""
    assert resolve_region(REGIONS, text="this message needs a price") is None


def test_matching_is_case_insensitive() -> None:
    assert resolve_region(REGIONS, text="ETA DUBAI").key == "uae"


def test_a_marker_ending_in_punctuation_still_matches() -> None:
    """"u.a.e." cannot be matched with \\b, which is why lookarounds are used."""
    assert resolve_region(REGIONS, text="delivery at Sharjah, U.A.E. next week").key == "uae"


# --------------------------------------------------------------------------- #
# An empty table
# --------------------------------------------------------------------------- #


def test_without_a_table_nothing_is_ever_routed() -> None:
    """Deleting the section from the YAML must disable forwarding, not crash it."""
    assert resolve_region({}, text="Dubai", mailbox="supply.uae@x.com") is None


def test_a_region_needs_no_rules_to_be_valid() -> None:
    """A desk configured with only an address is unreachable, never a crash."""
    assert resolve_region({"x": Region(forward_to="a@b.com")}, text="anything") is None


# --------------------------------------------------------------------------- #
# Who is copied
# --------------------------------------------------------------------------- #


def test_the_copy_list_arrives_from_the_environment() -> None:
    regions = Registries.load(
        REGISTRIES_PATH, region_cc={"uae": ["a@x.invalid", "b@x.invalid"]}
    ).regions

    assert regions["uae"].cc == ["a@x.invalid", "b@x.invalid"]


def test_the_committed_file_holds_no_copy_list() -> None:
    """Same rule as the desk addresses: real mailboxes never enter git."""
    shipped = Registries.load(REGISTRIES_PATH).regions
    assert all(region.cc == [] for region in shipped.values())


def test_an_empty_copy_list_is_a_setting_not_a_gap() -> None:
    """Blanking SG_CC must clear the list, not silently keep an older one."""
    regions = Registries.load(REGISTRIES_PATH, region_cc={"uae": []}).regions
    assert regions["uae"].cc == []
