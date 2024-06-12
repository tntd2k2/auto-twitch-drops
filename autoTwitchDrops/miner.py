import asyncio
import json
import logging

from . import Campaign, Channel
from .twitchsocket import TwitchWebSocket
from .utils import sort_campaigns
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
logger = logging.getLogger()
from aiohttp.client_exceptions import ServerDisconnectedError


class TwitchMiner:
    logger = logging.getLogger(__name__)

    def __init__(self, login, api, game=None):
        self.login = login
        self.api = api
        self.need_mine = True
        self.game = game
        self.current_channel = None
        self.current_game = None
        self.topics = [{
                "text": "user-drop-events.USER_ID",
                "type": "user_id",
            },
            {
                "text": "onsite-notifications.USER_ID",
                "type": "user_id",
            }]

    async def handle_websocket(self):
        while True:
            try:
                data = await self.websocket.receive_message()
            except Exception:
                logger.exception("Websocket error, reconnect.")
                self.websocket.connect()

            if not data or not data.get("message"):
                continue

            message = json.loads(data["message"])

            if data["topic"] == f"onsite-notifications.{self.login.user_id}":
                if message["type"] == "create-notification":
                    data = message["data"]["notification"]
                    if data["type"] == "user_drop_reward_reminder_notification":
                        self.need_mine = False
                    return

            if data["topic"] == f"broadcast-settings-update.{self.current_channel}":
                if message["type"] == "broadcast_settings_update":
                    self.current_game = message["game_id"]

            # if data["topic"] != f"user-drop-events.{self.login.user_id}":
            #     continue

            # message = json.loads(data["message"])

            # if message["type"] == "drop-progress":
            #     data = message["data"]
            #     if data["current_progress_min"] >= data["required_progress_min"]:
            #         self.need_mine = False

    async def run(self):
        self.logger.info("Please don't use Twitch while mining to avoid errors")
        self.logger.info("To track your drops progress: https://www.twitch.tv/drops/inventory")

        try:
            self.websocket = TwitchWebSocket(self.login, self.topics)

            await self.websocket.connect()

            asyncio.create_task(self.handle_websocket())

            asyncio.create_task(self.websocket.run_ping())

            while True:
                self.current_game = None
                streamer = await self.pick_streamer()

                try:
                    self.current_channel = streamer
                    await self.watch(streamer)
                    await self.websocket.switch_channel_topic(streamer.id)

                except RuntimeError: # Except if stream goes offline
                    self.logger.exception("Streamer seems changed game/go offline, switch.")
                    continue
                except ServerDisconnectedError:
                    self.logger.exception("Critical error while watching. Restarting.")
                    continue
                finally:
                    self.current_channel = None

        finally:
            await self.websocket.close()

    async def pick_streamer(self):
        await self.update_inventory()
        await self.update_campaigns()
        await self.claim_all_drops()
        self.campaigns = sort_campaigns(self.campaigns)

        while True:
            streamers = (await self.get_channel_to_mine())

            if streamers:
                break

            self.logger.info("No streamers to mine... We will continue in 60 seconds.")
            await asyncio.sleep(60)

        self.logger.debug(f"Founded streamers to mine: {[streamer.nickname for streamer in streamers]}")

        return streamers[0]

    async def watch(self, streamer):
        while self.need_mine:
            if self.current_game and self.current_game != self.current_channel.game["id"]:
                raise RuntimeError("Streamer changed game")

            await self.api.send_watch(streamer.nickname)
            self.logger.info(f"Watch sent to {streamer.nickname}")
            await asyncio.sleep(15)

        self.need_mine = True

    async def get_channel_to_mine(self):
        streamers = None

        for campaign in self.campaigns:
            if self.game and self.game != campaign.game["displayName"]:
                continue

            self.logger.debug(f"Founded campaign to mine: {campaign.name}")

            if campaign.channelsEnabled:
                streamers = await self.get_online_channels(campaign.channels, campaign.game["id"])

                if streamers:
                    break

            else:
                response = await self.api.get_category_streamers(campaign.game["slug"])

                streamers = [Channel(channel["node"]) for channel in response if channel["node"].get("broadcaster")] # we need to check because sometimes twitch give forbidden data
                if streamers:
                    break

        return streamers

        # return by campaign first available channel

    async def get_online_channels(self, channels, game_id):
        response = await self.api.get_channels_information(channels)
        response = [Channel(channel["user"]) for channel in response if channel["user"]["stream"] and channel["user"]["broadcastSettings"]["game"]]
        return list(filter(lambda x: x.game["id"] == game_id, response))

    async def update_inventory(self):
        inventory = await self.api.get_inventory()

        if inventory.get("dropCampaignsInProgress"):
            self.inventory = [Campaign(x) for x in inventory["dropCampaignsInProgress"]]
        else:
            self.inventory = []

        self.claimed_drops_ids = []

        for campaign in self.inventory:
            for drop in campaign.drops:
                if drop.claimed or drop.required_time <= drop.watched_time:
                    for benefit in drop.benefits_ids:
                        self.claimed_drops_ids.append(benefit)

        if inventory.get("gameEventDrops"): # We will use benefits ids, because that better way to check
            for drop in inventory["gameEventDrops"]:
                self.claimed_drops_ids.append(drop["id"])

        logger.info("Inventory updated")

    async def update_campaigns(self):
        # campaigns = list(filter(lambda x: x["status"] == "ACTIVE", await self.api.get_campaigns()))
        response = await self.api.get_campaigns()
        campaigns_ids = [campaign["id"] for campaign in response if campaign["status"] == "ACTIVE"]
        response = await self.api.get_full_campaigns_data(campaigns_ids)
        self.campaigns = [Campaign(x["user"]["dropCampaign"]) for x in response]

        campaigns_to_remove = []
        drops_to_remove = {}

        # Identify drops and campaigns to remove
        for i, campaign in enumerate(self.campaigns):

            drops_to_remove[i] = []
            for j, drop in enumerate(campaign.drops):
                for benefit in drop.benefits_ids:
                    if benefit in self.claimed_drops_ids:
                        logger.debug(f"Removed drop {drop.id_} Name: {drop.name}")
                        drops_to_remove[i].append(j)
                        break

        # Remove drops in reverse order to avoid indexing issues
        for i, drop_indices in drops_to_remove.items():
            for j in sorted(drop_indices, reverse=True):
                del self.campaigns[i].drops[j]

        # Remove empty campaigns
        for i, campaign in reversed(list(enumerate(self.campaigns))):
            if len(campaign.drops) == 0:
                logger.debug(f"Removed campaign {campaign.id_} Name: {campaign.name}")
                del self.campaigns[i]

        # Remove campaigns in reverse order to avoid indexing issues
        for i in sorted(campaigns_to_remove, reverse=True):
            del self.campaigns[i]

        logger.info(f"Campaigns updated - {len(self.campaigns)}")

    async def claim_all_drops(self):
        for campaign in self.inventory:
            for drop in campaign.drops:
                if drop.required_time <= drop.watched_time and drop.claimed is False:
                    await self.api.claim_drop(drop.instanceId)
                    logger.info(f"Claimed drop {drop.name}")
