"""Module for the DocRef cog."""
import asyncio
import shutil
import pathlib
import re
import tempfile
import urllib.parse
from typing import Dict, Iterator, List, Match, Optional, Tuple, cast

import aiohttp
import discord
import sphinx.util.inventory as sphinx_inv
from redbot.core import Config, checks, commands, data_manager
from redbot.core.utils import chat_formatting as chatutils

from .errors import (
    AlreadyUpToDate,
    Forbidden,
    HTTPError,
    InternalError,
    InvNotAvailable,
    NoMoreRefs,
    NotFound,
)
from .types import (
    FilterFunc,
    InvData,
    InvMetaData,
    MatchesDict,
    NodeRef,
    RawInvData,
    RawInvMetaData,
    RefDict,
    RefSpec,
)

UNIQUE_ID = 0x178AC710


class DocRef(commands.Cog):
    """Search for references on documentation webpages.

    I need to be able to embed links for this cog to be useful!
    """

    def __init__(self):
        super().__init__()
        self.conf: Config = Config.get_conf(
            self, identifier=UNIQUE_ID, force_registration=True
        )
        self.conf.register_global(sites={}, inv_metadata={})
        self.conf.register_guild(sites={})
        self.invs_data: Dict[str, InvData] = {}
        self.invs_dir: pathlib.Path = data_manager.cog_data_path(self) / "invs"
        self.invs_dir.mkdir(parents=True, exist_ok=True)
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

    @commands.command(aliases=["ref", "rtd", "rtfm"])
    async def docref(self, ctx: commands.Context, sitename: str, *, node_ref: NodeRef):
        """Search for a reference in documentation webpages.

        This will display a list hyperlinks to possible matches for the
        provided node reference.

        `<sitename>` is the name for the documentation webpage. This is set
        when the webpage is added with `[p]addsite`.

        `<node_ref>` is a reference to a sphinx node in reST syntax, however
        most of the syntactic operators can be omitted for a more vague
        reference.

        For example, all of these commands will return the same result:

            ``[p]docref pydocs :py:class:`int`\u200B``
            ``[p]docref pydocs :class:`int`\u200B``
            ``[p]docref pydocs :class:int``
            ``[p]docref pydocs `int`\u200B``
            ``[p]docref pydocs int``

        """
        # First we get the base URL and inventory data
        try:
            url, inv_data = await self.get_inv_data(sitename, ctx.guild)
        except InvNotAvailable:
            await ctx.send(f'Couldn\'t find the site name "{sitename}".')
            return
        except NotFound:
            await ctx.send(
                f'It appears as though the "{sitename}" site\'s URL is now a 404.'
            )
            return

        # Now we need to filter the data according to our node_ref

        filter_func: FilterFunc = self._get_filter_func(node_ref)

        reftypes: Iterator[str] = filter(filter_func, inv_data.keys())

        exact_matches: MatchesDict = {}
        partial_matches: MatchesDict = {}

        # If the reftype is bogus, the filter result will be empty
        # Thus, we'll never enter the loop
        valid_reftype = False

        for reftype in reftypes:
            valid_reftype = True

            ref_dict = inv_data[reftype]
            tup = self.get_matches(node_ref.refname, ref_dict)
            matches: List[RefSpec] = tup[0]
            exact: bool = tup[1]

            if not matches:
                continue

            if exact:
                assert matches  # just double check our subroutine didn't do a poopoo
                exact_matches[reftype] = matches
            elif exact_matches:
                # we've already found closer matches than these, discard
                continue
            else:
                partial_matches[reftype] = matches

        if not valid_reftype:
            await ctx.send(
                f"Couldn't find any references with the `:{node_ref.reftype}:` "
                f"directive."
            )
            return

        matches: MatchesDict = exact_matches or partial_matches

        if not matches:
            await ctx.send(
                f"Couldn't find any references matching ``{node_ref}\u200B``."
            )
            return

        metadata = await self.get_inv_metadata(url)
        embed_list = self._new_match_embed(
            metadata,
            matches,
            exact=bool(exact_matches),
            colour=await ctx.embed_colour(),
        )

        for embed in embed_list:
            await ctx.send(embed=embed)

    @commands.command()
    @checks.admin_or_permissions(administrator=True)
    async def addsite(self, ctx: commands.Context, sitename: str, url: str, scope=None):
        """Add a new documentation site.

        `<url>` must be resolved to an actual docs webpage, and not a redirect
        URL. For example, `https://docs.python.org` is invalid, however the
        URL it redirects to, `https://docs.python.org/3/`, is valid.

        `<scope>` is an owner-only argument and specifies where this site can
        be accessed from. Defaults to `server` for everyone except the bot
        owner, whose scope defaults to `global`.
        """
        if not url.startswith("https://"):
            await ctx.send("Must be an HTTPS URL.")
            return
        if not url.endswith("/"):
            url += "/"

        is_owner = await ctx.bot.is_owner(ctx.author)
        if scope is not None and not is_owner:
            await ctx.send("Only bot owners can specify the scope.")
            return
        elif scope is None:
            if is_owner:
                scope = "global"
            else:
                scope = "guild"

        scope = scope.lower()
        if scope in ("server", "guild"):
            if ctx.guild is None:
                await ctx.send(f"Can't add to {scope} scope from DM.")
                return
            conf_group = self.conf.guild(ctx.guild).sites
        elif scope == "global":
            conf_group = self.conf.sites
        else:
            await ctx.send(f'Unknown scope "{scope}".')
            return

        try:
            async with ctx.typing():
                await self.update_inv(url)
        except NotFound:
            await ctx.send("Couldn't find an inventory from that URL.")
            return
        except HTTPError as exc:
            await ctx.send(
                f"Something went wrong whilst trying to download the "
                f"inventory file. HTTP response code {exc.code}."
            )
            return
        else:
            existing_url = await conf_group.get_raw(sitename, default=None)
            if existing_url is not None:
                await self._decref(existing_url)

            await conf_group.set_raw(sitename, value=url)
            await self._incref(url)
            await ctx.tick()

    @commands.command(aliases=["removesite"])
    @checks.admin_or_permissions(administrator=True)
    async def delsite(self, ctx: commands.Context, sitename: str):
        """Remove a documentation site.

        This command will remove just one site, and if there are multiple
        sites with the same name, it will remove the most local one.

        Only bot owners can delete global sites.
        """
        is_owner = await ctx.bot.is_owner(ctx.author)
        try:
            await self.remove_site(sitename, ctx.guild, is_owner)
        except InvNotAvailable:
            await ctx.send(f"Couldn't find a site by the name `{sitename}`.")
        except Forbidden as exc:
            await ctx.send(exc.args[0])
        else:
            await ctx.tick()

    @commands.command()
    async def docsites(self, ctx: commands.Context):
        """List all installed and available documentation websites."""
        sites = await self.conf.sites()
        if ctx.guild is not None:
            sites.update(await self.conf.guild(ctx.guild).sites())

        lines: List[str] = []
        for name, url in sites.items():
            try:
                metadata = await self.get_inv_metadata(url)
            except InvNotAvailable:
                continue

            lines.append(f"`{name}` - [{metadata}]({url})")

        if not lines:
            await ctx.send("No sites are available.")

        description = "\n".join(lines)

        for page in chatutils.pagify(description, page_length=2048):
            await ctx.send(
                embed=discord.Embed(description=page, colour=await ctx.embed_colour())
            )

    @commands.command()
    @checks.is_owner()
    async def forceupdate(self, ctx: commands.Context, sitename: str):
        """Force a documentation webpage to be updated.

        Updates are checked for every time you use `[p]docref`. However,
        the inventory cache isn't actually updated unless we have an old
        version number.

        This command will force the site to be updated irrespective of the
        version number.
        """
        url: str = await self.get_url(sitename)
        if url is None:
            await ctx.send(f'Couldn\'t find the site name "{sitename}".')
            return
        try:
            async with ctx.typing():
                await self.update_inv(url, force=True)
        except NotFound:
            await ctx.send(
                f'It appears as though the "{sitename}" site\'s URL is now a 404.'
            )
        else:
            await ctx.tick()

    @staticmethod
    def get_matches(refname: str, ref_dict: RefDict) -> Tuple[List[RefSpec], bool]:
        """Get a list of matching references.

        First this function will look for exact matches (for which there will
        only be one), and if it can't find any, it will look for references
        whose name ends with the given ``refname``.

        Arguments
        ---------
        refname
            The name of the reference being looked for.
        ref_dict
            A mapping from references to `RefSpec` objects.

        Returns
        -------
        Tuple[List[RefSpec], bool]
            The `bool` will be ``True`` if the matches returned are exact.

        """
        # first look for an exact match
        if refname in ref_dict:
            return [ref_dict[refname]], True

        # look for references ending with the refname
        return (
            [
                ref_spec
                for cur_refname, ref_spec in ref_dict.items()
                if cur_refname.endswith(refname)
            ],
            False,
        )

    async def get_inv_data(
        self, site: str, guild: Optional[discord.Guild] = None
    ) -> Tuple[str, InvData]:
        """Get data for an inventory by its user-defined name and scope.

        Also updates the locally cached inventory if necessary.

        Returns
        -------
        Tuple[str, InvData]
            A tuple in the form (url, data).

        """
        url = await self.get_url(site, guild)
        if url is None:
            raise InvNotAvailable()
        await self.update_inv(url)
        return url, self.invs_data[url]

    async def get_url(
        self, sitename: str, guild: Optional[discord.Guild] = None
    ) -> Optional[str]:
        """Get a URL by its sitename and scope.

        Arguments
        ---------
        sitename : str
            The user-defined site name.
        guild : Optional[discord.Guild]
            The guild from who's data the URL is being retreived.

        Returns
        -------
        Optional[str]
            The URL for the requested site. ``None`` if no site is found.

        """
        if guild is not None:
            url = await self.conf.guild(guild).sites.get_raw(sitename, default=None)
            if url is not None:
                return url
        return await self.conf.sites.get_raw(sitename, default=None)

    async def remove_site(
        self, sitename: str, guild: Optional[discord.Guild], is_owner: bool
    ) -> None:
        """Remove a site from the given scope.

        Only removes one site at a time. If there is a site with the same name
        in both the guild and global scope, only the guild one will be
        removed.

        Arguments
        ---------
        sitename
            The user-defined site name.
        guild
            The guild from who's data is being mutated.
        is_owner
            Whether or not the user doing the action is the bot owner.

        Raises
        ------
        InvNotAvailable
            If no site with that name is available in the given scope.
        Forbidden
            If the user does not have the right privelages to remove the site.

        """
        url = await self.get_url(sitename, guild)
        if url is None:
            raise InvNotAvailable()

        if guild is not None:
            sites = await self.conf.guild(guild).sites()
            if sitename in sites:
                del sites[sitename]
                await self.conf.guild(guild).sites.set(sites)
                await self._decref(url)
                return

        if not is_owner:
            raise Forbidden("Only bot owners can delete global sites.")

        async with self.conf.sites() as sites:
            del sites[sitename]
        await self._decref(url)

    async def update_inv(self, url: str, *, force: bool = False) -> InvData:
        """Update a locally cached inventory.

        Unless ``force`` is ``True``, this won't update the cache unless the
        metadata for the inventory does not match.

        Arguments
        ---------
        url : str
            The URL for the docs website. This is the path to the webpage, and
            not to the inventory file.
        force : bool
            Whether or not we should force the update. Defaults to ``False``.

        Returns
        -------
        InvData
            The up-to-date data for the inventory.

        """
        try:
            data = await self.get_inv_from_url(url, force_update=force)
        except AlreadyUpToDate:
            try:
                data = self.invs_data[url]
            except KeyError:
                path = self._get_inv_path(url)
                data = self.load_inv_file(path, url)
                self.invs_data[url] = data
        else:
            self.invs_data[url] = data

        return data

    def _get_inv_path(self, url: str) -> pathlib.Path:
        return self.invs_dir / f"{safe_filename(url)}.inv"

    async def get_inv_from_url(
        self, url: str, *, force_update: bool = False
    ) -> InvData:
        """Gets inventory data from its URL.

        Arguments
        ---------
        url : str
            The URL for the docs website.
        force_update : bool
            Whether or not the inventory should be force updated. Defaults to
            ``False``.

        Returns
        -------
        InvData
            The data for the requested inventory.

        Raises
        ------
        AlreadyUpToDate
            If the inventory was already up to date, and ``force_update`` was
            ``False``.

        """
        inv_path = await self.download_inv_file(url, force_update=force_update)
        return self.load_inv_file(inv_path, url)

    def load_inv_file(self, file_path: pathlib.Path, url: str) -> InvData:
        """Load an inventory file from its filepath.

        Returns
        -------
        InvData
            The data from the inventory file.

        """
        inv_data = self._load_inv_file_raw(file_path, url)
        return self._format_raw_inv_data(inv_data)

    @staticmethod
    def _load_inv_file_raw(file_path: pathlib.Path, url: str) -> RawInvData:
        with file_path.open("rb") as stream:
            inv_data = sphinx_inv.InventoryFile.load(stream, url, urllib.parse.urljoin)
        return inv_data

    async def download_inv_file(
        self, url: str, *, force_update: bool = False
    ) -> pathlib.Path:
        """Download the inventory file from a URL.

        Arguments
        ---------
        url : str
            The URL for the docs website. This is the path to the webpage, and
            not to the inventory file.
        force_update : bool
            Whether or not the data should be forcibly updated. Defaults to
            ``False``.

        Raises
        ------
        AlreadyUpToDate
            If the local version matches that of the remote, and
            ``force_update`` is False.

        Returns
        -------
        pathlib.Path
            The path to the local inventory file.

        """
        inv_path = self._get_inv_path(url)
        inv_url = urllib.parse.urljoin(url, "objects.inv")
        async with self.session.get(inv_url) as resp:
            self._check_response(resp)
            # read header comments to get version
            header_lines: List[bytes] = []
            idx = 0
            async for line in resp.content:
                header_lines.append(cast(bytes, line))
                idx += 1
                if idx > 2:
                    break
        projname = header_lines[1].rstrip()[11:].decode()
        version = header_lines[2].rstrip()[11:].decode()
        metadata = InvMetaData(projname, version)
        if not force_update and await self._inv_metadata_matches(url, metadata):
            raise AlreadyUpToDate()

        fd, filename = tempfile.mkstemp()
        with open(fd, "wb") as stream:
            async with self.session.get(inv_url) as resp:
                chunk = await resp.content.read(1024)
                while chunk:
                    stream.write(chunk)
                    chunk = await resp.content.read(1024)
        shutil.move(filename, inv_path)

        await self.set_inv_metadata(url, metadata)

        return inv_path

    @staticmethod
    def _check_response(resp: aiohttp.ClientResponse) -> None:
        """Checks a response to an HTTP request and raises the appropriate error.

        Raises
        ------
        NotFound
            If the response code is 404.
        HTTPError
            If there was an unexpected response code.

        """
        if resp.status == 200:
            return
        elif resp.status == 404:
            error_cls = NotFound
        else:
            error_cls = HTTPError
        raise error_cls(resp.status, resp.reason, resp)

    async def _inv_metadata_matches(self, url: str, metadata: InvMetaData) -> bool:
        try:
            existing_metadata: InvMetaData = await self.get_inv_metadata(url)
        except InvNotAvailable:
            return False
        else:
            return metadata == existing_metadata

    async def get_inv_metadata(self, url: str) -> InvMetaData:
        """Get metadata for an inventory.

        Arguments
        ---------
        url : str
            The URL for the docs website.

        Returns
        -------
        InvMetaData
            The metadata for the inventory.

        Raises
        ------
        InvNotAvailable
            If there is no inventory matching that URL.

        """
        try:
            raw_metadata: RawInvMetaData = await self.conf.inv_metadata.get_raw(url)
        except KeyError:
            raise InvNotAvailable
        else:
            return InvMetaData(**raw_metadata)

    async def set_inv_metadata(self, url: str, metadata: InvMetaData) -> None:
        """Set metadata for an inventory.

        Arguments
        ---------
        url : str
            The URL for the docs website.
        metadata : InvMetaData
            The inventory's metadata.

        """
        await self.conf.inv_metadata.set_raw(url, value=metadata.to_dict())

    @staticmethod
    def _format_raw_inv_data(inv_data: RawInvData) -> InvData:
        ret: InvData = {}
        for ref_type, refs_dict in inv_data.items():
            new_refs_dict: RefDict = {}
            for ref_name, raw_ref_spec in refs_dict.items():
                ref_url: str = raw_ref_spec[2]
                display_name: str = raw_ref_spec[3]
                if display_name == "-":
                    display_name = ref_name
                else:
                    display_name = f"{ref_name} - {display_name}"
                new_refs_dict[ref_name] = RefSpec(ref_url, display_name)
            ret[ref_type] = new_refs_dict
        return ret

    @staticmethod
    def _new_match_embed(
        metadata: InvMetaData,
        matches: MatchesDict,
        *,
        exact: bool,
        colour: Optional[discord.Colour] = None,
    ) -> List[discord.Embed]:
        count = 0
        match_type = "exact" if exact else "possible"

        lines: List[str] = []
        for reftype, refspec_list in matches.items():
            lines.append(chatutils.bold(reftype))
            for refspec in refspec_list:
                count += 1
                # The zero-width space is necessary to make sure discord doesn't remove
                # leading spaces at the start of an embed.
                lines.append(
                    "\u200b" + (" " * 4) + f"[{refspec.display_name}]({refspec.url})"
                )

        plural = "es" if count > 1 else ""
        description = "\n".join(lines)
        ret: List[discord.Embed] = []

        for page in chatutils.pagify(description, page_length=2048):
            # my little hack to make sure pagify doesn't strip the initial indent
            if not page.startswith("**"):
                page = " " * 4 + page

            ret.append(
                discord.Embed(description=page, colour=colour or discord.Embed.Empty)
            )

        ret[0].title = f"Found {count} {match_type} match{plural}."
        ret[-1].set_footer(text=f"{metadata.projname} {metadata.version}")
        return ret

    @staticmethod
    def _get_filter_func(node_ref: NodeRef) -> FilterFunc:
        if node_ref.role == "any":

            if node_ref.lang is not None:
                # Some weirdo did a :lang:any: search

                def _filter(reftype: str) -> bool:
                    lang_and_role = reftype.split(":")
                    # This should return a sequence in the form [lang, role]
                    # But we should check and make sure just in case
                    if len(lang_and_role) != 2:
                        raise InternalError(
                            f"Unexpected reftype in inventory data {reftype}"
                        )

                    lang = lang_and_role[0]
                    return lang == node_ref.lang

            else:
                # If the role is just :any: we don't filter at all

                def _filter(_: str) -> bool:
                    return True

        elif node_ref.role and node_ref.lang:

            def _filter(reftype: str) -> bool:
                return reftype == f"{node_ref.lang}:{node_ref.role}"

        elif node_ref.role and not node_ref.lang:

            def _filter(reftype: str) -> bool:
                lang_and_role = reftype.split(":")
                if len(lang_and_role) != 2:
                    raise InternalError(
                        f"Unexpected reftype in inventory data {reftype}"
                    )

                role = lang_and_role[1]
                return node_ref.role == role

        else:
            # We shouldn't have got here
            raise InternalError(f"Unexpected NodeRef {node_ref!r}")

        return _filter

    async def _decref(self, url: str) -> None:
        metadata = await self.get_inv_metadata(url)
        try:
            metadata.dec_refcount()
        except NoMoreRefs:
            await self._destroy_inv(url)
        else:
            await self.set_inv_metadata(url, metadata)

    async def _incref(self, url: str) -> None:
        metadata = await self.get_inv_metadata(url)
        metadata.inc_refcount()
        await self.set_inv_metadata(url, metadata)

    async def _destroy_inv(self, url: str) -> None:
        async with self.conf.inv_metadata() as inv_metadata:
            del inv_metadata[url]
        try:
            del self.invs_data[url]
        except KeyError:
            pass
        inv_file = self._get_inv_path(url)
        if inv_file.exists():
            inv_file.unlink()

    def cog_unload(self) -> None:
        asyncio.create_task(self.session.close())


_INVALID_CHARSET = re.compile("[^A-z0-9_]")


def _replace_invalid_char(match: Match[str]) -> str:
    return str(ord(match[0]))


def safe_filename(instr: str) -> str:
    """Generates a filename-friendly string.

    Useful for creating filenames unique to URLs.
    """
    return "_" + _INVALID_CHARSET.sub(_replace_invalid_char, instr)
