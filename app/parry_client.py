"""Everything that talks to parry.gg lives here; the rest of the app only sees these dataclasses.

The real client is not exercised by the test suite (it needs an API key and network). Run
`python -m app.parry_client "<name>" [custom_slug]` to check discovery against the live API.
"""
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

from . import config


@dataclass
class EventInfo:
    id: str
    name: str
    game_name: str = ""
    entrant_count: int = 0
    completed: bool = True
    slug: str = ""


@dataclass
class TournamentInfo:
    id: str
    name: str
    slug: str = ""
    start_date: str = ""
    num_attendees: int = 0
    events: list = field(default_factory=list)


@dataclass
class Attendee:
    user_id: str
    gamer_tag: str
    event_ids: list = field(default_factory=list)


@dataclass
class PlacementInfo:
    user_id: str
    gamer_tag: str
    placement: int


class ParryClient(Protocol):
    def search_tournaments(self, name: str = "", custom_slug: str = "") -> list[TournamentInfo]: ...
    def get_tournament(self, identifier: str, is_id: bool = False) -> TournamentInfo: ...
    def get_attendees(self, tournament_id: str) -> list[Attendee]: ...
    def get_event_placements(self, event_id: str) -> list[PlacementInfo]: ...


class MissingApiKey(RuntimeError):
    pass


def parse_tournament_identifier(text: str) -> tuple[str, str]:
    """Accept a parry.gg URL, a slug, or a raw id. Returns ('slug'|'id', value)."""
    text = text.strip()
    m = re.search(r"parry\.gg/([^/?#\s]+)", text)
    if m:
        return "slug", m.group(1)
    if re.fullmatch(r"[0-9a-fA-F-]{32,36}", text):
        return "id", text
    return "slug", text


class GrpcParryClient:
    """Thin wrapper over the official `parrygg` package (gRPC, x-api-key metadata)."""

    def __init__(self):
        key = config.api_key()
        if not key:
            raise MissingApiKey("Set PARRY_API_KEY (or PARRY_API_KEY_FILE) to talk to parry.gg")
        import grpc
        self._grpc = grpc
        self._md = [("x-api-key", key)]
        self._channel = grpc.secure_channel(config.PARRY_HOST, grpc.ssl_channel_credentials())

    def close(self):
        self._channel.close()

    def _call(self, method, request):
        for attempt in range(4):
            try:
                return method(request, metadata=self._md, timeout=30)
            except self._grpc.RpcError as e:
                transient = e.code() in (self._grpc.StatusCode.UNAVAILABLE,
                                         self._grpc.StatusCode.DEADLINE_EXCEEDED,
                                         self._grpc.StatusCode.RESOURCE_EXHAUSTED)
                if not transient or attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    @staticmethod
    def _event(e) -> EventInfo:
        from parrygg.models import event_pb2
        done = e.state == event_pb2.EVENT_STATE_COMPLETED
        return EventInfo(e.id, e.name, e.game.name, e.entrant_count, done, e.slug)

    @staticmethod
    def _tournament(t) -> TournamentInfo:
        # A custom slug (e.g. a club's quick link) is shared by every tournament in the series and the
        # order of `slugs` isn't stable, so store the per-tournament slug, which ends with the id prefix.
        slugs = [s.slug for s in t.slugs]
        slug = next((s for s in slugs if s.endswith("-" + t.id[:8])), slugs[0] if slugs else "")
        start = t.start_date.ToDatetime().date().isoformat() if t.HasField("start_date") else ""
        events = [GrpcParryClient._event(e) for e in t.events]
        return TournamentInfo(t.id, t.name, slug, start, t.num_attendees, events)

    def search_tournaments(self, name: str = "", custom_slug: str = "") -> list[TournamentInfo]:
        from parrygg.models import filter_pb2, pagination_pb2
        from parrygg.services import tournament_service_pb2 as pb, tournament_service_pb2_grpc as rpc
        stub = rpc.TournamentServiceStub(self._channel)
        # Only set the filters in use: an explicitly empty field is sent as a filter that matches nothing.
        criteria = {k: v for k, v in (("name", name), ("custom_slug", custom_slug)) if v}
        found, cursor = [], None
        while True:
            page = pagination_pb2.PaginationRequest(page_size=100)
            if cursor is not None:
                page.cursor.CopyFrom(cursor)
            resp = self._call(stub.GetTournaments, pb.GetTournamentsRequest(
                filter=filter_pb2.TournamentsFilter(**criteria), pagination_request=page))
            found += [self._tournament(t) for t in resp.tournaments]
            if not resp.pagination_response.has_more:
                return found
            cursor = resp.pagination_response.next_cursor

    def get_tournament(self, identifier: str, is_id: bool = False) -> TournamentInfo:
        from parrygg.services import tournament_service_pb2 as pb, tournament_service_pb2_grpc as rpc
        kind, value = ("id", identifier) if is_id else parse_tournament_identifier(identifier)
        req = pb.GetTournamentRequest(id=value) if kind == "id" else pb.GetTournamentRequest(tournament_slug=value)
        info = self._tournament(self._call(rpc.TournamentServiceStub(self._channel).GetTournament, req).tournament)
        if not info.events:
            info.events = self._events_from_attendees(info.id)
        return info

    def _events_from_attendees(self, tournament_id: str) -> list[EventInfo]:
        from parrygg.services import event_service_pb2 as pb, event_service_pb2_grpc as rpc
        stub = rpc.EventServiceStub(self._channel)
        ids = {eid for a in self.get_attendees(tournament_id) for eid in a.event_ids}
        events = []
        for eid in sorted(ids):
            e = self._call(stub.GetEvent, pb.GetEventRequest(id=eid)).event
            events.append(self._event(e))
        return events

    def get_attendees(self, tournament_id: str) -> list[Attendee]:
        from parrygg.services import tournament_service_pb2 as pb, tournament_service_pb2_grpc as rpc
        resp = self._call(rpc.TournamentServiceStub(self._channel).GetTournamentAttendees,
                          pb.GetTournamentAttendeesRequest(tournament_id=tournament_id))
        return [Attendee(a.user.id, a.user.gamer_tag, [e.event_id for e in a.events]) for a in resp.attendees]

    def get_event_placements(self, event_id: str) -> list[PlacementInfo]:
        from parrygg.services import event_service_pb2 as pb, event_service_pb2_grpc as rpc
        resp = self._call(rpc.EventServiceStub(self._channel).GetEventPlacements,
                          pb.GetEventPlacementsRequest(id=event_id))
        out = []
        for p in resp.placements:
            if p.placement < 1:
                continue
            for u in p.event_entrant.entrant.users:
                out.append(PlacementInfo(u.id, u.gamer_tag, p.placement))
        return out


def matches_pattern(pattern: str, name: str, slug: str) -> bool:
    """Case/punctuation-insensitive substring match against name or slug."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    needle = norm(pattern)
    return bool(needle) and (needle in norm(name) or needle in norm(slug))


if __name__ == "__main__":
    # Live check: does the API's name filter find your tournaments? Prints no secrets.
    client = GrpcParryClient()
    for t in client.search_tournaments(*sys.argv[1:3]):
        print(t.id, "|", t.name, "|", t.slug, "|", t.start_date, "|", t.num_attendees, "attendees |", len(t.events), "events")
