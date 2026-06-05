from dataclasses import dataclass, field
from pathlib import Path
import os
import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class MeshCoreConfig:
    port: str = "/dev/ttyUSB0"
    channel: str = "#kriis"
    advert_interval_seconds: int = 3600
    bot_mention: str = "@kriisibot"


@dataclass
class WeatherConfig:
    enabled: bool = True
    poll_interval_seconds: int = 600
    api_key: str = ""


@dataclass
class EestiEeConfig:
    enabled: bool = True
    url: str = "https://api.app.eesti.ee/api/sitrep/v1/full-events"
    poll_interval_seconds: int = 300


@dataclass
class RssFeed:
    url: str
    name: str
    enabled: bool = True


@dataclass
class ClaudeConfig:
    model: str = "claude-haiku-4-5-20251001"
    max_response_chars: int = 200


@dataclass
class UserReportsConfig:
    report_trigger: str = "!raport"
    cooldown_seconds: int = 600
    max_stored: int = 50
    max_age_hours: int = 6
    session_timeout_minutes: int = 30


@dataclass
class TargetedAlertsConfig:
    enabled: bool = True
    radius_km: float = 50.0
    critical_event_types: list[str] = field(default_factory=lambda: ["air_raid", "hostile_drone", "chemical_hazard", "explosion"])


@dataclass
class DatabaseConfig:
    path: str = "events.db"


@dataclass
class Settings:
    meshcore: MeshCoreConfig = field(default_factory=MeshCoreConfig)
    eesti_ee: EestiEeConfig = field(default_factory=EestiEeConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    rss_feeds: list[RssFeed] = field(default_factory=list)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    event_taxonomy: list[str] = field(default_factory=lambda: ["other"])
    user_reports: UserReportsConfig = field(default_factory=UserReportsConfig)
    targeted_alerts: TargetedAlertsConfig = field(default_factory=TargetedAlertsConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    anthropic_api_key: str = ""


def load_settings(path: str = "settings.yaml") -> Settings:
    settings_path = Path(path)
    raw: dict = {}
    if settings_path.exists():
        with open(settings_path) as f:
            raw = yaml.safe_load(f) or {}

    mc_raw = raw.get("meshcore", {})
    meshcore = MeshCoreConfig(
        port=os.getenv("MESHCORE_PORT", mc_raw.get("port", "/dev/ttyUSB0")),
        channel=mc_raw.get("channel", "#kriis"),
        advert_interval_seconds=mc_raw.get("advert_interval_seconds", 3600),
        bot_mention=mc_raw.get("bot_mention", "@kriisibot"),
    )

    ee_raw = raw.get("eesti_ee", {})
    eesti_ee = EestiEeConfig(
        enabled=ee_raw.get("enabled", True),
        url=ee_raw.get("url", "https://api.app.eesti.ee/api/sitrep/v1/full-events"),
        poll_interval_seconds=ee_raw.get("poll_interval_seconds", 300),
    )

    rss_feeds = [
        RssFeed(url=f["url"], name=f["name"], enabled=f.get("enabled", True))
        for f in raw.get("rss_feeds", [])
    ]

    cl_raw = raw.get("claude", {})
    claude = ClaudeConfig(
        model=cl_raw.get("model", "claude-haiku-4-5-20251001"),
        max_response_chars=cl_raw.get("max_response_chars", 200),
    )

    taxonomy = raw.get("event_taxonomy", ["other"])

    ur_raw = raw.get("user_reports", {})
    user_reports = UserReportsConfig(
        report_trigger=ur_raw.get("report_trigger", "!raport"),
        cooldown_seconds=ur_raw.get("cooldown_seconds", 600),
        max_stored=ur_raw.get("max_stored", 50),
        max_age_hours=ur_raw.get("max_age_hours", 6),
        session_timeout_minutes=ur_raw.get("session_timeout_minutes", 30),
    )

    ta_raw = raw.get("targeted_alerts", {})
    targeted_alerts = TargetedAlertsConfig(
        enabled=ta_raw.get("enabled", True),
        radius_km=ta_raw.get("radius_km", 50.0),
        critical_event_types=ta_raw.get("critical_event_types", ["air_raid", "hostile_drone", "chemical_hazard", "explosion"]),
    )

    db_raw = raw.get("database", {})
    database = DatabaseConfig(path=db_raw.get("path", "events.db"))

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment or .env file")

    w_raw = raw.get("weather", {})
    weather = WeatherConfig(
        enabled=w_raw.get("enabled", True),
        poll_interval_seconds=w_raw.get("poll_interval_seconds", 600),
        api_key=w_raw.get("api_key", ""),
    )

    return Settings(
        meshcore=meshcore,
        eesti_ee=eesti_ee,
        weather=weather,
        targeted_alerts=targeted_alerts,
        rss_feeds=rss_feeds,
        claude=claude,
        event_taxonomy=taxonomy,
        user_reports=user_reports,
        database=database,
        anthropic_api_key=api_key,
    )
