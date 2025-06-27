# A part of NonVisual Desktop Access (NVDA)
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.
# Copyright (C) 2018-2023 NV Access Limited, Babbage B.V., Takuya Nishimoto

"""Default highlighter based on GDI Plus."""

from typing import Optional, Tuple

from autoSettingsUtils.autoSettings import SupportedSettingType
from autoSettingsUtils.driverSetting import BooleanDriverSetting
import vision
from vision.constants import Context
from vision.util import getContextRect
from vision.visionHandlerExtensionPoints import EventExtensionPoints
from vision import providerBase
from windowUtils import CustomWindow
import wx
from gui.settingsDialogs import (
	AutoSettingsMixin,
	SettingsPanel,
	VisionProviderStateControl,
)
import api
from ctypes import byref, WinError
from ctypes.wintypes import COLORREF, MSG
import winUser
from logHandler import log
from mouseHandler import getTotalWidthAndHeightAndMinimumPosition
from locationHelper import RectLTWH
from collections import namedtuple
import threading
from winAPI.messageWindow import WindowMessage
import winGDI
import weakref
from colors import RGB
import core
import windows_utils


class HighlightStyle(
	namedtuple("HighlightStyle", ("color", "width", "style", "margin")),
):
	"""Represents the style of a highlight for a particular context.
	@ivar color: The color to use for the style
	@type color: L{RGB}
	@ivar width: The width of the lines to be drawn, in pixels.
		A higher width reduces the inner dimensions of the rectangle.
		Therefore, if you need to increase the outer dimensions of the rectangle,
		you need to increase the margin as well.
	@type width: int
	@ivar style: The style of the lines to be drawn;
		One of the C{winGDI.DashStyle*} enumeration constants.
	@type style: int
	@ivar margin: The number of pixels between the highlight's rectangle
		and the rectangle of the object to be highlighted.
		A higher margin stretches the highlight's rectangle.
		This value may also be negative.
	@type margin: int
	"""


BLUE = RGB(0x03, 0x36, 0xFF)
PINK = RGB(0xFF, 0x02, 0x66)
YELLOW = RGB(0xFF, 0xDE, 0x03)
DASH_BLUE = HighlightStyle(BLUE, 5, winGDI.DashStyleDash, 5)
SOLID_PINK = HighlightStyle(PINK, 5, winGDI.DashStyleSolid, 5)
SOLID_BLUE = HighlightStyle(BLUE, 5, winGDI.DashStyleSolid, 5)
SOLID_YELLOW = HighlightStyle(YELLOW, 2, winGDI.DashStyleSolid, 2)


class MagnifyWindow(CustomWindow):
	transparency = 0xFF
	className = "NVDAMagnifier"
	windowName = "NVDA Magnifier Window"
	windowStyle = winUser.WS_POPUP | winUser.WS_DISABLED
	extendedWindowStyle = (
		# Ensure that the window is on top of all other windows
		winUser.WS_EX_TOPMOST
		# A layered window ensures that L{transparentColor} will be considered transparent, when painted
		| winUser.WS_EX_LAYERED
		# Ensure that the window can't be activated when pressing alt+tab
		| winUser.WS_EX_NOACTIVATE
		# Make this a transparent window,
		# primarily for accessibility APIs to ignore this window when getting a window from a screen point
		| winUser.WS_EX_TRANSPARENT
	)
	transparentColor = 0  # Black

	@classmethod
	def _get__wClass(cls):
		wClass = super()._wClass
		wClass.style = winUser.CS_HREDRAW | winUser.CS_VREDRAW
		wClass.hbrBackground = winGDI.gdi32.CreateSolidBrush(COLORREF(cls.transparentColor))
		return wClass

	def updateLocationForDisplays(self):
		if vision._isDebug():
			log.debug("Updating NVDAMagnifier window location for displays")
		displays = [wx.Display(i).GetGeometry() for i in range(wx.Display.GetCount())]
		screenWidth, screenHeight, minPos = getTotalWidthAndHeightAndMinimumPosition(displays)
		# Hack: Windows has a "feature" that will stop desktop shortcut hotkeys from working
		# when a window is full screen.
		# Removing one line of pixels from the bottom of the screen will fix this.
		left = minPos.x
		top = minPos.y
		width = screenWidth
		height = screenHeight - 1
		self.location = RectLTWH(left, top, width, height)
		winUser.user32.ShowWindow(self.handle, winUser.SW_HIDE)
		if not winUser.user32.SetWindowPos(
			self.handle,
			winUser.HWND_TOPMOST,
			left,
			top,
			width,
			height,
			winUser.SWP_NOACTIVATE,
		):
			raise WinError()
		winUser.user32.ShowWindow(self.handle, winUser.SW_SHOWNA)

	def __init__(self, magnifier):
		if vision._isDebug():
			log.debug("initializing NVDAMagnifier window")
		super().__init__(
			windowName=self.windowName,
			windowStyle=self.windowStyle,
			extendedWindowStyle=self.extendedWindowStyle,
		)
		self.location = None
		self.magnifierRef = weakref.ref(magnifier)
		winUser.SetLayeredWindowAttributes(
			self.handle,
			self.transparentColor,
			self.transparency,
			winUser.LWA_ALPHA | winUser.LWA_COLORKEY,
		)
		self.updateLocationForDisplays()
		if not winUser.user32.UpdateWindow(self.handle):
			raise WinError()

	def windowProc(self, hwnd, msg, wParam, lParam):
		if msg == winUser.WM_PAINT:
			self._paint()
			# Ensure the window is top most
			winUser.user32.SetWindowPos(
				self.handle,
				winUser.HWND_TOPMOST,
				0,
				0,
				0,
				0,
				winUser.SWP_NOACTIVATE | winUser.SWP_NOMOVE | winUser.SWP_NOSIZE,
			)
		elif msg == winUser.WM_DESTROY:
			winUser.user32.PostQuitMessage(0)
		elif msg == winUser.WM_TIMER:
			self.refresh()
		elif msg == WindowMessage.DISPLAY_CHANGE:
			# wx might not be aware of the display change at this point
			core.callLater(100, self.updateLocationForDisplays)

	def _paint(self):
		magnifier = self.magnifierRef()
		if not magnifier:
			# The magnifier instance died unexpectedly, kill the window as well
			winUser.user32.PostQuitMessage(0)
			return
		contextRects = {}
		for context in magnifier.enabledContexts:
			rect = magnifier.contextToRectMap.get(context)
			if not rect:
				continue
			elif context == Context.NAVIGATOR and contextRects.get(Context.FOCUS) == rect:
				# When the focus overlaps the navigator object, which is usually the case,
				# show a different highlight style.
				# Focus is in contextRects, do not show the standalone focus highlight.
				contextRects.pop(Context.FOCUS)
				# Navigator object might be in contextRects as well
				contextRects.pop(Context.NAVIGATOR, None)
				context = Context.FOCUS_NAVIGATOR
			contextRects[context] = rect
		if not contextRects:
			return
		with winUser.paint(self.handle) as hdc:
			with winGDI.GDIPlusGraphicsContext(hdc) as graphicsContext:
				for context, rect in contextRects.items():
					HighlightStyle = magnifier._ContextStyles[context]
					# Before calculating logical coordinates,
					# make sure the rectangle falls within the magnifier window
					rect = rect.intersection(self.location)
					try:
						rect = rect.toLogical(self.handle)
					except RuntimeError:
						log.debugWarning("", exc_info=True)
					rect = rect.toClient(self.handle)
					try:
						rect = rect.expandOrShrink(HighlightStyle.margin)
					except RuntimeError:
						pass
					with winGDI.GDIPlusPen(
						HighlightStyle.color.toGDIPlusARGB(),
						HighlightStyle.width,
						HighlightStyle.style,
					) as pen:
						winGDI.gdiPlusDrawRectangle(graphicsContext, pen, *rect.toLTWH())

	def refresh(self):
		winUser.user32.InvalidateRect(self.handle, None, True)


_contextOptionLabelsWithAccelerators = {
	# Translators: shown for a magnifier setting that toggles
	# highlighting the system focus.
	Context.FOCUS: _("Highlight system fo&cus"),
	# Translators: shown for a magnifier setting that toggles
	# highlighting the browse mode cursor.
	Context.BROWSEMODE: _("Highlight browse &mode cursor"),
	# Translators: shown for a magnifier setting that toggles
	# highlighting the navigator object.
	Context.NAVIGATOR: _("Highlight navigator &object"),
}

_supportedContexts = (Context.FOCUS, Context.NAVIGATOR, Context.BROWSEMODE)


class NVDAMagnifierSettings(providerBase.VisionEnhancementProviderSettings):
	# Default settings for parameters
	magnifyFocus = False
	magnifyNavigator = False
	magnifyBrowseMode = False

	@classmethod
	def getId(cls) -> str:
		return "NVDAMagnifier"

	@classmethod
	def getDisplayName(cls) -> str:
		# Translators: Description for NVDA's built-in screen magnifier.
		return _("Visual Magnifier")

	def _get_supportedSettings(self) -> SupportedSettingType:
		return [
			BooleanDriverSetting(
				"magnify%s" % (context[0].upper() + context[1:]),
				_contextOptionLabelsWithAccelerators[context],
				defaultVal=True,
			)
			for context in _supportedContexts
		]


class NVDAMagnifierGuiPanel(
	AutoSettingsMixin,
	SettingsPanel,
):
	_enableCheckSizer: wx.BoxSizer
	_enabledCheckbox: wx.CheckBox

	helpId = "VisionSettingsFocusMagnify"

	def __init__(
		self,
		parent: wx.Window,
		providerControl: VisionProviderStateControl,
	):
		self._providerControl = providerControl
		initiallyEnabledInConfig = NVDAMagnifier.isEnabledInConfig()
		if not initiallyEnabledInConfig:
			settingsStorage = self._getSettingsStorage()
			settingsToCheck = [
				settingsStorage.magnifyBrowseMode,
				settingsStorage.magnifyFocus,
				settingsStorage.magnifyNavigator,
			]
			if any(settingsToCheck):
				log.debugWarning(
					"Magnifier disabled in config while some of its settings are enabled. "
					"This will be corrected",
				)
				settingsStorage.magnifyBrowseMode = False
				settingsStorage.magnifyFocus = False
				settingsStorage.magnifyNavigator = False
		super().__init__(parent)

	def _buildGui(self):
		self.mainSizer = wx.BoxSizer(wx.VERTICAL)

		self._enabledCheckbox = wx.CheckBox(
			self,
			#  Translators: The label for a checkbox that enables / disables focus magnification
			#  in the NVDA magnifier vision settings panel.
			label=_("&Enable Magnification"),
			style=wx.CHK_3STATE,
		)

		self.mainSizer.Add(self._enabledCheckbox)
		self.mainSizer.AddSpacer(size=self.scaleSize(10))
		# this options separator is done with text rather than a group box because a groupbox is too verbose,
		# but visually some separation is helpful, since the rest of the options are really sub-settings.
		self.optionsText = wx.StaticText(
			self,
			# Translators: The label for a group box containing the NVDA magnifier options.
			label=_("Options:"),
		)
		self.mainSizer.Add(self.optionsText)

		self.lastControl = self.optionsText
		self.settingsSizer = wx.BoxSizer(wx.VERTICAL)
		self.makeSettings(self.settingsSizer)
		self.mainSizer.Add(self.settingsSizer, border=self.scaleSize(15), flag=wx.LEFT | wx.EXPAND)
		self.mainSizer.Fit(self)
		self.SetSizer(self.mainSizer)

	def getSettings(self) -> NVDAMagnifierSettings:
		# AutoSettingsMixin uses the getSettings method (via getSettingsStorage) to get the instance which is
		# used to get / set attributes. The attributes must match the id's of the settings.
		# We want them set on our settings instance.
		return VisionEnhancementProvider.getSettings()

	def makeSettings(self, sizer: wx.BoxSizer):
		self.updateDriverSettings()
		# bind to all check box events
		self.Bind(wx.EVT_CHECKBOX, self._onCheckEvent)
		self._updateEnabledState()

	def onPanelActivated(self):
		self.lastControl = self.optionsText

	def _updateEnabledState(self):
		settingsStorage = self._getSettingsStorage()
		settingsToTriggerActivation = [
			settingsStorage.magnifyBrowseMode,
			settingsStorage.magnifyFocus,
			settingsStorage.magnifyNavigator,
		]
		isAnyEnabled = any(settingsToTriggerActivation)
		if all(settingsToTriggerActivation):
			self._enabledCheckbox.Set3StateValue(wx.CHK_CHECKED)
		elif isAnyEnabled:
			self._enabledCheckbox.Set3StateValue(wx.CHK_UNDETERMINED)
		else:
			self._enabledCheckbox.Set3StateValue(wx.CHK_UNCHECKED)

		if not self._ensureEnableState(isAnyEnabled) and isAnyEnabled:
			self._onEnableFailure()

	def _onEnableFailure(self):
		"""Initialization of magnifier failed. Reset settings / GUI"""
		settingsStorage = self._getSettingsStorage()
		settingsStorage.magnifyBrowseMode = False
		settingsStorage.magnifyFocus = False
		settingsStorage.magnifyNavigator = False
		self.updateDriverSettings()
		self._updateEnabledState()

	def _ensureEnableState(self, shouldBeEnabled: bool) -> bool:
		currentlyEnabled = bool(self._providerControl.getProviderInstance())
		if shouldBeEnabled and not currentlyEnabled:
			windows_utils.run_mag()
			windows_utils.set_level()
			log.debug("Running Mag")
			return self._providerControl.startProvider()
		elif not shouldBeEnabled and currentlyEnabled:
			return self._providerControl.terminateProvider()
		return True

	def _onCheckEvent(self, evt: wx.CommandEvent):
		settingsStorage = self._getSettingsStorage()
		if evt.GetEventObject() is self._enabledCheckbox:
			isEnableAllChecked = evt.IsChecked()
			settingsStorage.magnifyBrowseMode = isEnableAllChecked
			settingsStorage.magnifyFocus = isEnableAllChecked
			settingsStorage.magnifyNavigator = isEnableAllChecked
			if not self._ensureEnableState(isEnableAllChecked) and isEnableAllChecked:
				self._onEnableFailure()
			self.updateDriverSettings()
		else:
			self._updateEnabledState()

		providerInst: Optional[NVDAMagnifier] = self._providerControl.getProviderInstance()
		if providerInst:
			providerInst.refresh()


class NVDAMagnifier(providerBase.VisionEnhancementProvider):
	_ContextStyles = {
		Context.FOCUS: DASH_BLUE,
		Context.NAVIGATOR: SOLID_PINK,
		Context.FOCUS_NAVIGATOR: SOLID_BLUE,
		Context.BROWSEMODE: SOLID_YELLOW,
	}
	_refreshInterval = 100
	customWindowClass = MagnifyWindow
	_settings = NVDAMagnifierSettings()
	_window: Optional[customWindowClass] = None
	enabledContexts: Tuple[Context]  # type info for autoprop: L{_get_enableContexts}

	@classmethod  # override
	def getSettings(cls) -> NVDAMagnifierSettings:
		return cls._settings

	@classmethod  # override
	def getSettingsPanelClass(cls):
		"""Returns the class to be used in order to construct a settings panel for the provider.
		@return: Optional[SettingsPanel]
		@remarks: When None is returned, L{gui.settingsDialogs.VisionProviderSubPanel_Wrapper} is used.
		"""
		return NVDAMagnifierGuiPanel

	@classmethod  # override
	def canStart(cls) -> bool:
		return True

	def registerEventExtensionPoints(  # override
		self,
		extensionPoints: EventExtensionPoints,
	) -> None:
		extensionPoints.post_focusChange.register(self.handleFocusChange)
		extensionPoints.post_reviewMove.register(self.handleReviewMove)
		extensionPoints.post_browseModeMove.register(self.handleBrowseModeMove)

	def __init__(self):
		super().__init__()
		log.debug("Starting NVDAMagnifier")
		self.contextToRectMap = {}
		winGDI.gdiPlusInitialize()
		self._magnifierThread = threading.Thread(
			name=f"{self.__class__.__module__}.{self.__class__.__qualname__}",
			target=self._run,
			daemon=True,
		)
		self._magnifierRunningEvent = threading.Event()
		self._magnifierThread.start()
		# Make sure the magnifier thread doesn't exit early.
		waitResult = self._magnifierRunningEvent.wait(0.2)
		if waitResult is False or not self._magnifierThread.is_alive():
			raise RuntimeError("Magnifier thread wasn't able to initialize correctly")

	def terminate(self):
		log.debug("Terminating NVDAMagnifier")
		if self._magnifierThread and self._window and self._window.handle:
			if not winUser.user32.PostThreadMessageW(self._magnifierThread.ident, winUser.WM_QUIT, 0, 0):
				raise WinError()
			else:
				self._magnifierThread.join()
			self._magnifierrThread = None
		winGDI.gdiPlusTerminate()
		self.contextToRectMap.clear()
		super().terminate()

	def _run(self):
		try:
			if vision._isDebug():
				log.debug("Starting NVDAMagnifier thread")

			window = self._window = self.customWindowClass(self)
			timer = winUser.WinTimer(window.handle, 0, self._refreshInterval, None)
			self._magnifierRunningEvent.set()  # notify main thread that initialisation was successful
			msg = MSG()
			while (res := winUser.getMessage(byref(msg), None, 0, 0)) > 0:
				winUser.user32.TranslateMessage(byref(msg))
				winUser.user32.DispatchMessageW(byref(msg))
			if res == -1:
				# See the return value section of
				# https://docs.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getmessage
				raise WinError()
			if vision._isDebug():
				log.debug("Quit message received on NVDANagnifier thread")
			timer.terminate()
			window.destroy()
		except Exception:
			log.exception("Exception in NVDA Magnifier thread")

	def updateContextRect(self, context, rect=None, obj=None):
		"""Updates the position rectangle of the highlight for the specified context.
		If rect is specified, the method directly writes the rectangle to the contextToRectMap.
		Otherwise, it will call L{getContextRect}
		"""
		log.debug("update called")
		if context not in self.enabledContexts:
			log.debug("context not in enabled")
			return
		if rect is None:
			try:
				rect = getContextRect(context, obj=obj)
			except (LookupError, NotImplementedError, RuntimeError, TypeError):
				rect = None
				log.debug("rect=none")
		self.contextToRectMap[context] = rect
		log.debug("rect updated")
		if rect:
			log.debug("adjusting")
			log.debug(rect.top, rect.left)
			windows_utils.set_pos(int(rect.top), int(rect.left))

	def handleFocusChange(self, obj):
		self.updateContextRect(context=Context.FOCUS, obj=obj)
		if not api.isObjectInActiveTreeInterceptor(obj):
			self.contextToRectMap.pop(Context.BROWSEMODE, None)
		else:
			self.handleBrowseModeMove()

	def handleReviewMove(self, context):
		self.updateContextRect(context=Context.NAVIGATOR)

	def handleBrowseModeMove(self, obj=None):
		self.updateContextRect(context=Context.BROWSEMODE)

	def refresh(self):
		"""Refreshes the screen positions of the enabled highlights."""
		if self._window and self._window.handle:
			self._window.refresh()

	def _get_enabledContexts(self):
		"""Gets the contexts for which the magnifier is enabled."""
		return tuple(
			context
			for context in _supportedContexts
			if getattr(self.getSettings(), "magnify%s" % (context[0].upper() + context[1:]))
		)


VisionEnhancementProvider = NVDAMagnifier
