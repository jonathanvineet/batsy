package dev.batsy.widgets

import android.app.AlarmManager
import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.PowerManager
import android.os.SystemClock
import org.json.JSONObject

/**
 * Keeps the widgets fresh and handles their buttons.
 *
 * While the screen is on: every 30 s. Screen off: every 5 min, with a non-waking alarm, so a sleeping phone isn't
 * woken up for it at all, and the first refresh after you turn the screen on happens straight away.
 */
class Refresh : BroadcastReceiver() {

    override fun onReceive(c: Context, intent: Intent) {
        val pending = goAsync()
        Thread {
            try {
                if (intent.action == ACTION_BUTTON) press(c, intent)
                update(c)
            } catch (_: Exception) {
            } finally {
                schedule(c)
                pending.finish()
            }
        }.start()
    }

    private fun press(c: Context, intent: Intent) {
        val path = intent.getStringExtra("path") ?: return
        val action = intent.getStringExtra("action") ?: return
        if (action == "refresh") return  // just fetch again (update() runs next)
        Render.note(c, "…")
        updateFrom(c, Hub.cached(c))  // show the button was pressed before the network answers
        val said = Hub.post(c, path, JSONObject().put("action", action))
        Render.note(c, said)
    }

    companion object {
        const val ACTION_TICK = "dev.batsy.widgets.TICK"
        const val ACTION_BUTTON = "dev.batsy.widgets.BUTTON"

        fun widgetIds(c: Context): Pair<IntArray, IntArray> {
            val m = AppWidgetManager.getInstance(c)
            return m.getAppWidgetIds(ComponentName(c, DashboardWidget::class.java)) to
                m.getAppWidgetIds(ComponentName(c, MusicWidget::class.java))
        }

        /** Fetch and redraw every widget. Runs on a background thread. */
        fun update(c: Context) = updateFrom(c, Hub.fetch(c))

        fun updateFrom(c: Context, r: Hub.Result) {
            val m = AppWidgetManager.getInstance(c)
            val (dash, music) = widgetIds(c)
            for (id in dash) m.updateAppWidget(id, Render.dashboard(c, r))
            for (id in music) m.updateAppWidget(id, Render.music(c, r))
        }

        /** Refresh in the background now (from the UI thread). */
        fun now(c: Context) {
            c.sendBroadcast(Intent(c, Refresh::class.java).setAction(ACTION_TICK))
        }

        fun button(c: Context, path: String, action: String, request: Int): PendingIntent =
            PendingIntent.getBroadcast(
                c, request,
                Intent(c, Refresh::class.java).setAction(ACTION_BUTTON).putExtra("path", path).putExtra("action", action),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

        fun schedule(c: Context) {
            val (dash, music) = widgetIds(c)
            val am = c.getSystemService(AlarmManager::class.java)
            val pi = PendingIntent.getBroadcast(
                c, 1, Intent(c, Refresh::class.java).setAction(ACTION_TICK),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            if (dash.isEmpty() && music.isEmpty()) {
                am.cancel(pi)
                return
            }
            val on = c.getSystemService(PowerManager::class.java).isInteractive
            val delay = if (on) 30_000L else 5 * 60_000L
            // ELAPSED_REALTIME (not _WAKEUP): never wakes the phone; delivered as soon as it's awake again
            am.set(AlarmManager.ELAPSED_REALTIME, SystemClock.elapsedRealtime() + delay, pi)
        }
    }
}

class Boot : BroadcastReceiver() {
    override fun onReceive(c: Context, intent: Intent) = Refresh.now(c)
}

/** `adb shell am broadcast -n dev.batsy.widgets/.PairReceiver -a dev.batsy.widgets.PAIR --es token T --es remote URL` */
class PairReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, intent: Intent) {
        Hub.save(c, intent.getStringExtra("token"), intent.getStringExtra("remote"))
        Refresh.now(c)
    }
}
