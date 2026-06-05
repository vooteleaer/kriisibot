import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
import anthropic

from event_db import Event

if TYPE_CHECKING:
    from conversation import Turn

logger = logging.getLogger(__name__)

# ── System prompts ────────────────────────────────────────────────────────────

CHANNEL_SYSTEM = """\
Sa oled Eesti kriisiinfobot, mis töötab MeshCore raadiovõrgus avalikul kanalil.

Reeglid:
- Vasta ALATI samas keeles, milles kasutaja kirjutab
- AINULT LIHTTEKST — ei mingit markdown-i, tärne, sümboleid ega erimärgendeid
- IGA LAUSE peab mahtuma {max_chars} tähemärki — see on raadiokanali paketi piir
- Kirjuta lühikesi, täielikke lauseid. Lõpeta iga lause punkti, hüüumärgi või küsimärgiga
- Kui vastus vajab mitut lauset, kirjuta iga lause eraldi, koos lõpumärgiga
- Põhine ametlikel allikatel (eesti.ee, uudistevood) — need on usaldusväärsed
- Kasutajate raportid on KONTROLLIMATA — viita neile alati kui "kontrollimata"
- Kui puudub info, ütle seda ausalt — ära spekuleerita
- Kui küsimus on ebaselge, esita ÜKS lühike täpsustavküsimus
- TEEMA PIIRANG: vasta ainult kriisi- ja hädaolukordade kohta. Muu teema: "Saan aidata ainult kriisiinfo asjus."\
"""

PM_SYSTEM = """\
Sa oled Eesti kriisiinfobot erasõnumite vestluses MeshCore kasutajaga.

Aktiivsed sündmused:
{events_context}

Kontrollimata kasutajaraportid:
{reports_context}

Reeglid:
- Vasta ALATI samas keeles, milles kasutaja kirjutab
- Küsimustele vasta lühidalt, kasutades ametlikke allikaid
- Kasutajate raportid on KONTROLLIMATA — märgi seda alati
- Kui kasutaja kirjeldab sündmust, kogu detailid küsides vajaduse korral täpsustavküsimusi
  (küsi ükshaaval, nii palju küsimusi kui vaja — see on eravestlus)
- Asukoha küsimisel aktsepteeri ka ligikaudseid kirjeldusi (linn, linnaosa, maantee, \
  lähedal olev koht, tuntud objekt). Kui kasutaja ei tea täpset aadressi, küsi lähimat \
  tuntud kohta ("mis on lähim tänav / asutus / küla?"). Pane location väljale parim saadaolev kirjeldus.
- Kui oled kogunud piisavalt infot (vähemalt: mis juhtus + kus), lisa oma vastuse LÕPPU
  täpselt uuel real (midagi muud sinna vahele ei tohi tulla):
  RAPORT_JSON:{{"event_type":"<tüüp>","location":"<asukoht>","description":"<kirjeldus>","status":"OPEN"}}
- AINULT LIHTTEKST — ei mingit markdown-i ega tärnimärgendeid
- IGA LAUSE peab olema lühike ja täielik — lõpeta iga lause punkti, hüüumärgi või küsimärgiga
- Lisa RAPORT_JSON AINULT siis, kui kasutaja aktiivselt raporteerib sündmust — mitte küsimustele vastates
- RAPORT_JSON description väljast eemalda KÕIK isiklikud andmed: nimed, telefoninumbrid, \
  isikuandmed, vigastuste/tervise üksikasjad konkreetsete isikute kohta, täpsed koduaadressid. \
  Kirjelda sündmust üldiselt (nt "vigastatud isikud kohal" mitte "Jaan Tamm, 45a, murdumine")
- TEEMA PIIRANG: vasta ainult kriisi- ja hädaolukordade kohta. Muu teema puhul ütle lühidalt \
  "Saan aidata ainult kriisiinfo asjus." ja lõpeta vestlus\
"""

# ── Internal prompts ──────────────────────────────────────────────────────────

EXTRACT_PROMPT = """\
Loe järgmine tekst ja tuvasta:
1. Sündmuse tüüp kategooriate hulgast: {taxonomy}
2. Asukoht (linn, maakond vms) — kui on
3. Staatus: OPEN / CLOSED / UNKNOWN

Vasta JSON-ina:
{{"event_type": "...", "location": "...", "status": "..."}}

Tekst:
{raw_text}
"""


PLAUSIBILITY_PROMPT = """\
Hinda, kas järgmine kasutaja raport on usutav kriisiinfo Eesti kontekstis.
Vasta AINULT ühe sõnaga: jah / kahtlane / ei

Raport:
{report_text}
"""

DUPLICATE_PROMPT = """\
Kas uus sündmus räägib samast intsidendist kui olemasolev sündmus?
Vasta AINULT: jah / ei

Olemasolev:
{existing}

Uus:
{new_text}
"""

ANSWER_PROMPT = """\
Kasutaja küsimus: {question}

Aktiivsed kriisisündmused:
{events_context}

Kontrollimata kasutajaraportid:
{user_reports_context}

Vasta lühidalt — teised kasutajad näevad seda kanalil samuti.
"""

ALERT_PROMPT = """\
Koosta üks lühike kriisiteavitus (max {max_chars} tähemärki) järgmise sündmuse kohta.
Alusta tüübiga suurtähtedega, nt: "DROONIOHT:" või "ÜLEUJUTUS:" jne.
Ainult olulisim info — asukoht, olek, mida teha.

OLULINE: eemalda kõik isiklikud andmed (nimed, isikuandmed, vigastuste üksikasjad konkreetsete \
inimeste kohta, täpsed koduaadressid). Kasuta üldisi kirjeldusi, nt "vigastatud isikud" mitte nimesid.

Sündmus:
{event_text}
"""

_RAPORT_JSON_EXTRACT = re.compile(r"RAPORT_JSON:(\{.+\})", re.DOTALL)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_events(events: list[Event]) -> str:
    if not events:
        return "Aktiivseid sündmusi ei ole."
    lines = []
    for e in events:
        parts = [f"[{e.event_type or 'other'}]", e.title or e.description or e.raw_text[:80]]
        if e.location:
            parts.append(f"({e.location})")
        trust = "" if e.trust_level == "official" else f" [{e.trust_level}]"
        lines.append(" ".join(parts) + trust)
    return "\n".join(lines)


def _format_user_reports(reports: list[dict]) -> str:
    if not reports:
        return "Kasutajaraporteid ei ole."
    return "\n".join(
        f"[KONTROLLIMATA] {r['pubkey_prefix']}: {r['text']}" for r in reports
    )


def _extract_raport_json(text: str) -> "tuple[str, dict | None]":
    """Strip RAPORT_JSON marker from reply text and return (clean_text, fields|None)."""
    m = _RAPORT_JSON_EXTRACT.search(text)
    if not m:
        return text.strip(), None
    try:
        fields = json.loads(m.group(1))
        clean = text[: text.rfind("RAPORT_JSON:")].rstrip()
        return clean, fields
    except (json.JSONDecodeError, ValueError):
        return text.strip(), None


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ClassifyResult:
    event_type: str
    location: Optional[str]
    status: str


# ── Client ────────────────────────────────────────────────────────────────────

class ClaudeClient:
    def __init__(self, api_key: str, model: str, max_response_chars: int, taxonomy: list[str]):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.max_chars = max_response_chars
        self.taxonomy = taxonomy
        self._taxonomy_str = ", ".join(taxonomy)
        self._channel_system = CHANNEL_SYSTEM.format(max_chars=max_response_chars)

    def _channel_system_block(self) -> list[dict]:
        return [{"type": "text", "text": self._channel_system, "cache_control": {"type": "ephemeral"}}]

    async def _call(self, system: list[dict], messages: list[dict], max_tokens: int = 256) -> str:
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        return resp.content[0].text.strip()

    # ── Channel Q&A ──────────────────────────────────────────────────────────

    async def answer_question(
        self,
        question: str,
        active_events: list[Event],
        user_reports: list[dict],
        history: "list[Turn] | None" = None,
    ) -> str:
        context_block = ANSWER_PROMPT.format(
            question=question,
            events_context=_format_events(active_events),
            user_reports_context=_format_user_reports(user_reports),
        )
        messages: list[dict] = []
        if history:
            for turn in history[:-1]:
                messages.append({"role": turn.role, "content": turn.text})
        messages.append({"role": "user", "content": context_block})
        # Let full sentences through — chunker splits at sentence boundaries
        return await self._call(self._channel_system_block(), messages, max_tokens=500)

    # ── PM unified conversation ───────────────────────────────────────────────

    async def pm_turn(
        self,
        turns: list[dict],
        active_events: list[Event],
        user_reports: list[dict],
    ) -> "tuple[str, dict | None]":
        """One turn in a PM conversation.

        Returns (reply_text, report_fields) where report_fields is non-None
        when Claude has gathered enough to save a report.
        """
        system_text = PM_SYSTEM.format(
            events_context=_format_events(active_events),
            reports_context=_format_user_reports(user_reports),
        )
        system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        raw = await self._call(system, turns, max_tokens=500)
        reply, fields = _extract_raport_json(raw)
        return reply, fields

    # ── Classification / deduplication ───────────────────────────────────────

    async def classify_event(self, raw_text: str) -> ClassifyResult:
        prompt = EXTRACT_PROMPT.format(taxonomy=self._taxonomy_str, raw_text=raw_text[:800])
        try:
            result = await self._call(
                [{"type": "text", "text": "Vasta ainult JSON-iga."}],
                [{"role": "user", "content": prompt}],
                max_tokens=128,
            )
            # Extract JSON from response, ignoring markdown fences or surrounding text
            m = re.search(r"\{[^{}]+\}", result, re.DOTALL)
            if not m:
                raise ValueError("no JSON object found")
            data = json.loads(m.group())
            event_type = data.get("event_type", "other")
            if event_type not in self.taxonomy:
                event_type = "other"
            return ClassifyResult(
                event_type=event_type,
                location=data.get("location") or None,
                status=data.get("status", "UNKNOWN"),
            )
        except Exception:
            logger.warning("classify_event parse failed for: %s", raw_text[:60])
            return ClassifyResult(event_type="other", location=None, status="UNKNOWN")

    async def check_plausibility(self, report_text: str) -> str:
        prompt = PLAUSIBILITY_PROMPT.format(report_text=report_text[:400])
        try:
            answer = await self._call(
                [{"type": "text", "text": "Vasta ainult ühe sõnaga."}],
                [{"role": "user", "content": prompt}],
                max_tokens=8,
            )
            answer = answer.lower().strip(".")
            return answer if answer in ("jah", "kahtlane", "ei") else "kahtlane"
        except Exception:
            return "kahtlane"

    async def check_duplicate(self, new_raw: str, existing: Event) -> bool:
        existing_summary = f"{existing.title or ''} {existing.description or ''} {existing.location or ''}".strip()
        prompt = DUPLICATE_PROMPT.format(existing=existing_summary[:400], new_text=new_raw[:400])
        try:
            answer = await self._call(
                [{"type": "text", "text": "Vasta ainult jah või ei."}],
                [{"role": "user", "content": prompt}],
                max_tokens=8,
            )
            return answer.lower().strip(".") == "jah"
        except Exception:
            return False

    async def alert_for_event(self, event: Event) -> str:
        """One short alert message for a single new event."""
        event_text = f"{event.title or ''} {event.description or ''} {event.location or ''}".strip()
        if not event_text:
            event_text = event.raw_text[:300]
        prompt = ALERT_PROMPT.format(max_chars=self.max_chars, event_text=event_text[:400])
        alert = await self._call(
            self._channel_system_block(),
            [{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        return alert
