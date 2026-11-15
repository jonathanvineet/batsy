package dev.batsy.widgets

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context

/** Everything: Batsy, Claude limits, the laptop and PC, and Spotify. */
class DashboardWidget : AppWidgetProvider() {
    override fun onUpdate(c: Context, m: AppWidgetManager, ids: IntArray) {
        val cached = Hub.cached(c)
        for (id in ids) m.updateAppWidget(id, Render.dashboard(c, cached))
        Refresh.now(c)
    }

    override fun onDisabled(c: Context) = Refresh.schedule(c)
}

/** Spotify: cover, song, and play/pause, previous and next. */
class MusicWidget : AppWidgetProvider() {
    override fun onUpdate(c: Context, m: AppWidgetManager, ids: IntArray) {
        val cached = Hub.cached(c)
        for (id in ids) m.updateAppWidget(id, Render.music(c, cached))
        Refresh.now(c)
    }

    override fun onDisabled(c: Context) = Refresh.schedule(c)
}
