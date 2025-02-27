import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from sys import exit, stderr
from time import sleep
from typing import Any, Dict, List, Literal, Optional, Self, Union

import httpx
from httpx import Response
from loguru import logger
from plexapi.audio import TrackSession
from plexapi.media import Media
from plexapi.myplex import MyPlexAccount, MyPlexResource, PlexServer
from plexapi.video import EpisodeSession, MovieSession, Show
from pypresence import Presence


class Perplex:
    """
    Discord Rich Presence implementation for Plex.

    https://github.com/EthanC/Perplex
    """

    def Initialize(self: Self) -> None:
        """Initialize Perplex and begin primary functionality."""

        logger.info("Perplex")
        logger.info("https://github.com/EthanC/Perplex")

        self.config: Dict[str, Any] = Perplex.LoadConfig(self)

        Perplex.SetupLogging(self)

        plex: MyPlexAccount = Perplex.LoginPlex(self)
        discord: Presence = Perplex.LoginDiscord(self)

        Perplex.timer = None
        Perplex.viewOffset = None

        while True:
            session: Optional[
                Union[MovieSession, EpisodeSession, TrackSession]
            ] = Perplex.FetchSession(self, plex)

            if session:
                logger.success(f"Fetched active media session")

                if type(session) is MovieSession:
                    status: Dict[str, Any] = Perplex.BuildMoviePresence(self, session)
                elif type(session) is EpisodeSession:
                    status: Dict[str, Any] = Perplex.BuildEpisodePresence(self, session)
                elif type(session) is TrackSession:
                    status: Dict[str, Any] = Perplex.BuildTrackPresence(self, session)

                if Perplex.IsInPause(self, session, plex):
                    logger.info("Media session is paused")
                    status: Dict[str, Any] = Perplex.BuildPausePresence(self, session)

                success: bool = Perplex.SetPresence(self, discord, status)

                # Reestablish a failed Discord Rich Presence connection
                if not success:
                    discord = Perplex.LoginDiscord(self)
            else:
                try:
                    discord.clear()
                except Exception as e:
                    logger.error(f"An error occured while clearing status, {e}")

            # Presence updates have a rate limit of 1 update per 15 seconds
            # https://discord.com/developers/docs/rich-presence/how-to#updating-presence
            logger.info("Sleeping for 15s...")

            sleep(15.0)

    def LoadConfig(self: Self) -> Dict[str, Any]:
        """Load the configuration values specified in config.json"""

        try:
            with open("config.json", "r") as file:
                config: Dict[str, Any] = json.loads(file.read())
        except Exception as e:
            logger.critical(f"Failed to load configuration, {e}")

            exit(1)

        logger.success("Loaded configuration")

        return config

    def SetupLogging(self: Self) -> None:
        """Setup the logger using the configured values."""

        settings: Dict[str, Any] = self.config["logging"]

        if (level := settings["severity"].upper()) != "DEBUG":
            try:
                logger.remove()
                logger.add(stderr, level=level)

                logger.success(f"Set logger severity to {level}")
            except Exception as e:
                # Fallback to default logger settings
                logger.add(stderr, level="DEBUG")

                logger.error(f"Failed to set logger severity to {level}, {e}")

    def LoginPlex(self: Self) -> MyPlexAccount:
        """Authenticate with Plex using the configured credentials."""

        settings: Dict[str, Any] = self.config["plex"]

        account: Optional[MyPlexAccount] = None

        if Path("auth.txt").is_file():
            try:
                with open("auth.txt", "r") as file:
                    auth: str = file.read()

                account = MyPlexAccount(token=auth)
            except Exception as e:
                logger.error(f"Failed to authenticate with Plex using token, {e}")

        if not account:
            username: str = settings["username"]
            password: str = settings["password"]

            if settings["twoFactor"]:
                print(f"Enter Verification Code: ", end="")
                code: str = input()

                if (code == "") or (code.isspace()):
                    logger.warning(
                        "Two-Factor Authentication is enabled but code was not supplied"
                    )
                else:
                    password = f"{password}{code}"

            try:
                account = MyPlexAccount(username, password)
            except Exception as e:
                logger.critical(f"Failed to authenticate with Plex, {e}")

                exit(1)

        logger.success("Authenticated with Plex")

        try:
            with open("auth.txt", "w+") as file:
                file.write(account.authenticationToken)
        except Exception as e:
            logger.error(
                f"Failed to save Plex authentication token for future logins, {e}"
            )

        return account

    def LoginDiscord(self: Self) -> Presence:
        """Authenticate with Discord using the configured credentials."""

        client: Optional[Presence] = None

        while not client:
            try:
                client = Presence(self.config["discord"]["appId"])
                client.connect()
            except Exception as e:
                logger.error(f"Failed to connect to Discord ({e}) retry in 15s...")

                sleep(15.0)

        logger.success("Authenticated with Discord")

        return client

    def ConnectPlexMediaServer(
        self: Self, client: MyPlexAccount
    ) -> Optional[PlexServer]:
        settings: Dict[str, Any] = self.config["plex"]

        resource: Optional[MyPlexResource] = None

        for entry in settings["servers"]:
            for result in client.resources():
                if entry.lower() == result.name.lower():
                    resource = result

                    break

            if resource:
                break

        if not resource:
            logger.critical("Failed to locate configured Plex Media Server")

            exit(1)

        try:
            return resource.connect()
        except Exception as e:
            logger.critical(
                f"Failed to connect to configured Plex Media Server ({resource.name}), {e}"
            )

    def FetchSession(
        self: Self, client: MyPlexAccount
    ) -> Optional[Union[MovieSession, EpisodeSession, TrackSession]]:
        """
        Connect to the configured Plex Media Server and return the active
        media session.
        """
        settings: Dict[str, Any] = self.config["plex"]

        server = Perplex.ConnectPlexMediaServer(self, client)

        sessions: List[Media] = server.sessions()
        active: Optional[Union[MovieSession, EpisodeSession, TrackSession]] = None

        if len(sessions) > 0:
            i: int = 0

            for entry in settings["users"]:
                for result in sessions:
                    if entry.lower() in [alias.lower() for alias in result.usernames]:
                        active = sessions[i]

                        break

                    i += 1

        if not active:
            logger.info("No active media sessions found for configured users")

            return

        if type(active) is MovieSession:
            return active
        elif type(active) is EpisodeSession:
            return active
        elif type(active) is TrackSession:
            return active

        logger.error(f"Fetched active media session of unknown type: {type(active)}")

    def IsInPause(self: Self, active: Union[MovieSession, EpisodeSession, TrackSession], client: MyPlexAccount) -> bool:
        """Check if the active media session is paused."""
        active = Perplex.FetchSession(self, client)
        if active:
            result = Perplex.viewOffset == active.viewOffset
            Perplex.viewOffset = active.viewOffset
            return result

    def BuildPausePresence(self: Self, active: Union[MovieSession, EpisodeSession, TrackSession]) -> Dict[str, Any]:
        if isinstance(active, MovieSession):
            result = Perplex.BuildMoviePresence(self, active)
        elif isinstance(active, EpisodeSession):
            result = Perplex.BuildEpisodePresence(self, active)
        elif isinstance(active, TrackSession):
            result = Perplex.BuildTrackPresence(self, active)
        else:
            logger.error(f"Unknown session type: {type(active)}")
            return

        progress = active.viewOffset / active.duration * 100
        result["secondary"] = f"Paused ⏸︎ | {progress:.0f}%/{active.duration / 1000 / 60:.0f} min"
        result["remaining"] = -1

        return result

    def BuildMoviePresence(self: Self, active: MovieSession) -> Dict[str, Any]:
        """Build a Discord Rich Presence status for the active movie session."""

        minimal: bool = self.config["discord"]["minimal"]

        result: Dict[str, Any] = {}

        metadata: Optional[Dict[str, Any]] = Perplex.FetchMetadata(
            self, active.title, active.year, "movie", active
        )

        if minimal:
            result["primary"] = active.title
        else:
            result["primary"] = f"{active.title} ({active.year})"

            details: List[str] = []

            if len(active.genres) > 0:
                details.append(active.genres[0].tag)

            if len(active.directors) > 0:
                details.append(f"Dir. {active.directors[0].tag}")

            if len(details) > 1:
                result["secondary"] = ", ".join(details)

        if not metadata:
            # Default to image uploaded via Discord Developer Portal
            result["image"] = "movie"
            result["buttons"] = []
        else:
            mId: int = metadata["id"]
            mType: str = metadata["media_type"]
            imgPath: str = metadata["poster_path"]
            traktId: str = metadata.get("trakt")

            if traktId:
                result["image"] = f"https://image.tmdb.org/t/p/original{imgPath}" if imgPath else "tv"
                result["buttons"] = [
                    {"label": "Trakt.tv", "url": f"https://trakt.tv/{mType+'s'}/{mId}"}
                ]
            else:
                result["image"] = f"https://image.tmdb.org/t/p/original{imgPath}"
                result["buttons"] = [
                    {"label": "TMDB", "url": f"https://themoviedb.org/{mType}/{mId}"}
                ]

        result["remaining"] = int((active.duration / 1000) - (active.viewOffset / 1000))
        result["imageText"] = active.title

        logger.trace(result)

        return result

    def BuildEpisodePresence(self: Self, active: EpisodeSession) -> Dict[str, Any]:
        """Build a Discord Rich Presence status for the active episode session."""

        result: Dict[str, Any] = {}

        metadata: Optional[Dict[str, Any]] = Perplex.FetchMetadata(
            self, active.show().title, active.show().year, "tv", active
        )

        result["primary"] = active.show().title
        result["secondary"] = active.title
        result["remaining"] = int((active.duration / 1000) - (active.viewOffset / 1000))
        result["imageText"] = active.show().title

        if (active.seasonNumber) and (active.episodeNumber):
            result["secondary"] += f" (S{active.seasonNumber}:E{active.episodeNumber})"

        if not metadata:
            # Default to image uploaded via Discord Developer Portal
            result["image"] = "tv"
            result["buttons"] = []
        else:
            mId: int = metadata["id"]
            mType: str = metadata["media_type"]
            imgPath: str = metadata["poster_path"]
            traktId: str = metadata.get("trakt")

            if traktId:
                result["image"] = f"https://image.tmdb.org/t/p/original{imgPath}" if imgPath else "tv"
                result["buttons"] = [
                    {"label": "Trakt.tv", "url": f"https://trakt.tv/{mType+'s'}/{mId}"}
                ]
            else:
                result["image"] = f"https://image.tmdb.org/t/p/original{imgPath}"
                result["buttons"] = [
                    {"label": "TMDB", "url": f"https://themoviedb.org/{mType}/{mId}"}
                ]

        logger.trace(result)

        return result

    def BuildTrackPresence(self: Self, active: TrackSession) -> Dict[str, Any]:
        """Build a Discord Rich Presence status for the active music session."""

        result: Dict[str, Any] = {}

        result["primary"] = active.titleSort
        result["secondary"] = f"by {active.artist().title}"
        result["remaining"] = int((active.duration / 1000) - (active.viewOffset / 1000))
        result["imageText"] = active.parentTitle

        # Default to image uploaded via Discord Developer Portal
        result["image"] = "music"
        result["buttons"] = []

        logger.trace(result)

        return result

    def FetchMetadata(
        self: Self, title: str, year: int, format: str, session: Union[MovieSession, EpisodeSession]
    ) -> Optional[Dict[str, Any]]:
        """Fetch metadata for the provided title from TMDB."""

        settings: Dict[str, Any] = self.config["tmdb"]
        key: str = settings["apiKey"]

        # if title has a "(year)" in it, removes it. https://github.com/EthanC/Perplex/issues/21
        title = re.sub(r"\(\d{4}\)", "", title).strip()

        if settings["enable"]:

            try:
                res: Response = httpx.get(
                    f"https://api.themoviedb.org/3/search/multi?api_key={key}&query={urllib.parse.quote(title)}"
                )
                res.raise_for_status()

                logger.debug(f"(HTTP {res.status_code}) GET {res.url}")
                logger.trace(res.text)
            except Exception as e:
                logger.error(f"Failed to fetch metadata for {title} ({year}), {e}")

                return

        data: Dict[str, Any] = res.json()

        session.guids = session.source().guids # https://github.com/pkkid/python-plexapi/issues/1214
        media_type: Literal['episode', 'movie'] = "episode" if isinstance(session, EpisodeSession) else "movie"

        if session.guids and self.config["trakt"]["enabled"] and self.config["trakt"]["clientId"]: # if trakt is enabled we use it
            database: str = session.guids[0].id.split(":")[0]
            guid = session.guids[0].id.split("//")[-1]
            headers = {"Content-Type": "application/json", "trakt-api-version": "2", "trakt-api-key": self.config["trakt"]["clientId"]}
            try:
                res: Response = httpx.get(
                    f"https://api.trakt.tv/search/{database}/{guid}?type={media_type}", headers=headers
                )
                res.raise_for_status()

                logger.debug(f"(HTTP {res.status_code}) GET {res.url}")
                logger.trace(res.text)
            except Exception as e:
                logger.error(f"Failed to fetch metadata for {title} ({year}) from Trakt, {e}")
                res = None

            if (res:=res.json()):
                tmdb_guid: int = res[0][media_type]["ids"].get("tmdb")
                poster_path = None
                if tmdb_guid:
                    if media_type == "episode":
                        url = f"https://api.themoviedb.org/3/tv/{res[0]['show']['ids']['tmdb']}?api_key={key}"
                    else:
                        url = f"https://api.themoviedb.org/3/movie/{tmdb_guid}?api_key={key}"

                    res2: Response = httpx.get(url)
                    res2.raise_for_status()

                    if res2.json():
                        if media_type == "episode":
                            for season in res2.json()["seasons"]:
                                if season["season_number"] == res[0]["episode"]["season"]:
                                    poster_path = season["poster_path"]
                                    break
                        else:
                            poster_path = res2.json()["poster_path"]

                # We filter only the needed data with correct key names
                return {"id": res[0][media_type]["ids"]["trakt"], "media_type": media_type, "poster_path": poster_path, "trakt": True}
            # else we fallback to default search method


        if not settings["enable"]:
            logger.warning("TMDB disabled, some features will not be available")

            return

        if tmdb_guid:= [guid.id.split("//")[-1] for guid in session.guids if "tmdb" in guid.id][0]:
            plex: PlexServer = Perplex.ConnectPlexMediaServer(self, Perplex.LoginPlex(self))
            show: Show = plex.fetchItem(session.grandparentRatingKey)
            if media_type == "episode":
                if tmdb_guid:= [guid.id.split("//")[-1] for guid in show.guids if "tmdb" in guid.id][0]:
                    url = f"https://api.themoviedb.org/3/tv/{tmdb_guid}?api_key={key}"
            else:
                url = f"https://api.themoviedb.org/3/movie/{tmdb_guid}?api_key={key}"

            res: Response = httpx.get(url)
            res.raise_for_status()

            if res.json():
                data = {"results": [res.json()]}
                # we need to add the media_type to the data
                data["results"][0]["media_type"] = "tv" if media_type == "episode" else "movie"
        else:
            tmdb_guid = None

        for entry in data.get("results", []):
            if entry["id"] == tmdb_guid: # We found the correct entry
                break
            if format == "movie":
                if entry["media_type"] != format:
                    continue
                elif title.lower() != entry["title"].lower():
                    continue
                elif not entry["release_date"].startswith(str(year)):
                    continue
            elif format == "tv":
                if entry["media_type"] != format:
                    continue
                elif title.lower() != entry["name"].lower():
                    continue
                elif not entry["first_air_date"].startswith(str(year)):
                    continue

            return entry

        logger.warning(f"Could not locate metadata for {title} ({year})")

    def SetPresence(self: Self, client: Presence, data: Dict[str, Any]) -> bool:
        """Set the Rich Presence status for the provided Discord client."""

        title: str = data["primary"]

        data["buttons"].append(
            {"label": "Get Perplex", "url": "https://github.com/EthanC/Perplex"}
        )

        try:
            if data["remaining"] == -1:
                if Perplex.timer is None:
                    Perplex.timer = int(datetime.now().timestamp())
                client.update(
                    details=title,
                    state=data.get("secondary"),
                    start=Perplex.timer,
                    large_image=data["image"],
                    large_text=data["imageText"],
                    small_image="plex",
                    small_text="Plex",
                    buttons=data["buttons"],
                )
            else:
                Perplex.timer = None
                client.update(
                    details=title,
                    state=data.get("secondary"),
                    end=int(datetime.now().timestamp() + data["remaining"]),
                    large_image=data["image"],
                    large_text=data["imageText"],
                    small_image="plex",
                    small_text="Plex",
                    buttons=data["buttons"],
                )
        except Exception as e:
            logger.error(f"Failed to set Discord Rich Presence to {title}, {e}")

            return False

        logger.success(f"Set Discord Rich Presence to {title}")

        return True


if __name__ == "__main__":
    try:
        Perplex.Initialize(Perplex)
    except KeyboardInterrupt:
        exit()
