package dev.batsy.widgets

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.view.View
import android.widget.RemoteViews
import org.json.JSONObject
import java.io.File
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Turns Batsy's /api/status into the widgets' RemoteViews. */
object Render {
    private val SIGNAL = Color.parseColor("#FFC20E")
    private val EMBER = Color.parseColor("#FF9A1F")
    private val ALARM = Color.parseColor("#FF5C4D")
    private val GOOD = Color.parseColor("#5BD38A")
    private val STEEL = Color.parseColor("#5A6170")

    /** The answer to the last button press, shown for a few seconds in place of the activity line. */
    fun note(c: Context, text: String) {
        c.getSharedPreferences("batsy", Context.MODE_PRIVATE).edit()
            .putString("note", text).putLong("note_at", System.currentTimeMillis()).apply()
    }

    private fun recentNote(c: Context): String? {
        val p = c.getSharedPreferences("batsy", Context.MODE_PRIVATE)
        return if (System.currentTimeMillis() - p.getLong("note_at", 0) < 8000) p.getString("note", null) else null
    }

    private fun openApp(c: Context, request: Int, ask: Boolean = false): PendingIntent =
        PendingIntent.getActivity(
            c, request, Intent(c, MainActivity::class.java).putExtra("ask", ask)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

    // --- the dashboard ------------------------------------------------------------------------------------
    fun dashboard(c: Context, r: Hub.Result): RemoteViews {
        val v = RemoteViews(c.packageName, R.layout.widget_dashboard)
        val s = r.status
        v.setOnClickPendingIntent(R.id.root, openApp(c, 10))
        v.setOnClickPendingIntent(R.id.btn_stop, Refresh.button(c, "/api/batsy", "stop", 11))
        v.setOnClickPendingIntent(R.id.btn_wake, Refresh.button(c, "/api/pc", "wake", 12))
        v.setOnClickPendingIntent(R.id.btn_ask, openApp(c, 13, ask = true))
        v.setOnClickPendingIntent(R.id.btn_refresh, Refresh.button(c, "/api/none", "refresh", 14))
        musicButtons(c, v, 20)

        // header: state and how we're connected
        val live = r.error == null
        v.setTextViewText(R.id.route, when {
            live -> "${r.route} · ${clock(System.currentTimeMillis())}"
            s != null -> "offline · ${clock((s.optLong("t") * 1000))}"
            else -> "offline"
        })
        v.setTextColor(R.id.route, if (live) Color.parseColor("#8D95A5") else ALARM)

        if (s == null) {
            v.setTextViewText(R.id.state, "Batsy")
            v.setInt(R.id.dot, "setColorFilter", STEEL)
            v.setTextViewText(R.id.line_main, r.error ?: "Connecting…")
            v.setTextViewText(R.id.line_sub, "Plug into the PC, or connect Tailscale")
            v.removeAllViews(R.id.m1)
            v.removeAllViews(R.id.m2)
            return v
        }
        val b = s.optJSONObject("batsy") ?: JSONObject()
        val state = b.optString("state", "idle")
        v.setTextViewText(R.id.state, "Batsy · " + when (state) {
            "listening" -> "Listening"; "thinking" -> "Thinking"; "speaking" -> "Speaking"; else -> "Idle"
        })
        v.setInt(R.id.dot, "setColorFilter", if (!live) STEEL else when (state) {
            "listening" -> GOOD; "thinking" -> EMBER; "speaking" -> SIGNAL; else -> STEEL
        })

        // the activity line: a button's answer, else what it's doing, else what it said last
        val agents = b.optJSONArray("agents")
        val asks = b.optJSONArray("asks")
        val main = recentNote(c) ?: when {
            b.optString("status").isNotEmpty() -> b.optString("status")
            asks != null && asks.length() > 0 -> "Waiting for you: " + asks.optString(0)
            b.optString("said").isNotEmpty() -> b.optString("said")
            agents != null && agents.length() > 0 -> "Working: " + agents.optString(0)
            else -> "Say “Batsy”, or tap Ask"
        }
        v.setTextViewText(R.id.line_main, main)
        val sub = mutableListOf<String>()
        if (b.optString("you").isNotEmpty()) sub += "You: “${b.optString("you")}”"
        if (agents != null && agents.length() > 0 && !main.startsWith("Working")) sub += "${agents.length()} running"
        b.optJSONObject("next_reminder")?.let { sub += "⏰ ${it.optString("what")} ${clock(it.optLong("at") * 1000)}" }
        v.setTextViewText(R.id.line_sub, sub.joinToString("  ·  "))

        // Claude
        val cl = s.optJSONObject("claude") ?: JSONObject()
        bar(v, R.id.session_bar, R.id.session_pct, cl.optJSONObject("session"))
        bar(v, R.id.week_bar, R.id.week_pct, cl.optJSONObject("week"))
        val reset = cl.optJSONObject("session")?.optDouble("resets_at", 0.0) ?: 0.0
        v.setTextViewText(R.id.claude_reset, if (reset > 0) "session resets ${clock((reset * 1000).toLong())}" else
            "${cl.optInt("requests_week")} requests this week")

        // machines
        val machines = s.optJSONArray("machines")
        v.removeAllViews(R.id.m1)
        v.removeAllViews(R.id.m2)
        if (machines != null) {
            if (machines.length() > 0) v.addView(R.id.m1, machine(c, machines.getJSONObject(0)))
            if (machines.length() > 1) v.addView(R.id.m2, machine(c, machines.getJSONObject(1)))
            v.setViewVisibility(R.id.m2, if (machines.length() > 1) View.VISIBLE else View.GONE)
        }

        song(c, v, s.optJSONObject("spotify"), compact = true)
        return v
    }

    private fun machine(c: Context, m: JSONObject): RemoteViews {
        val v = RemoteViews(c.packageName, R.layout.machine)
        v.setTextViewText(R.id.m_name, m.optString("name"))
        val online = m.opt("online")
        if (online == false || online == null || online == JSONObject.NULL) {
            v.setInt(R.id.m_dot, "setColorFilter", if (online == false) ALARM else STEEL)
            v.setTextViewText(R.id.m_temp, "")
            v.setProgressBar(R.id.m_cpu_bar, 100, 0, false)
            v.setProgressBar(R.id.m_ram_bar, 100, 0, false)
            v.setTextViewText(R.id.m_cpu, "–")
            v.setTextViewText(R.id.m_ram, "–")
            v.setTextViewText(R.id.m_extra, if (online == false) "Off or asleep · tap Wake PC" else "Checking…")
            return v
        }
        v.setInt(R.id.m_dot, "setColorFilter", GOOD)
        val temp = m.optInt("temp", 0)
        v.setTextViewText(R.id.m_temp, if (temp > 0) "$temp°C" else "")
        v.setTextColor(R.id.m_temp, if (temp >= 85) ALARM else if (temp >= 70) EMBER else Color.parseColor("#8D95A5"))
        val cpu = m.optDouble("cpu", 0.0)
        val ram = m.optDouble("ram", 0.0)
        v.setProgressBar(R.id.m_cpu_bar, 100, cpu.toInt(), false)
        v.setProgressBar(R.id.m_ram_bar, 100, ram.toInt(), false)
        v.setTextViewText(R.id.m_cpu, "${cpu.toInt()}%")
        v.setTextViewText(R.id.m_ram, "${ram.toInt()}%")
        val extra = mutableListOf<String>()
        if (!m.isNull("battery")) extra += "${m.optInt("battery")}%" + if (m.optBoolean("charging")) "⚡" else " bat"
        m.optJSONObject("gpu")?.let { extra += "GPU ${it.optInt("util")}%" }
        if (!m.isNull("disk_free_gb")) extra += "${m.optInt("disk_free_gb")} GB free"
        extra += "↓${speed(m.optInt("net_down_kbps"))}"
        v.setTextViewText(R.id.m_extra, extra.joinToString(" · "))
        return v
    }

    private fun bar(v: RemoteViews, barId: Int, textId: Int, o: JSONObject?) {
        if (o == null || o.isNull("pct")) {
            v.setProgressBar(barId, 100, 0, false)
            v.setTextViewText(textId, "–")
            return
        }
        val pct = o.optDouble("pct", 0.0)
        v.setProgressBar(barId, 100, pct.toInt().coerceIn(0, 100), false)
        v.setTextViewText(textId, "${pct.toInt()}%")
        v.setTextColor(textId, if (pct >= 100) ALARM else if (pct >= 80) EMBER else Color.parseColor("#EEE8DA"))
    }

    // --- Spotify ---------------------------------------------------------------------------------------------
    fun music(c: Context, r: Hub.Result): RemoteViews {
        val v = RemoteViews(c.packageName, R.layout.widget_music)
        v.setOnClickPendingIntent(R.id.root, openApp(c, 30))
        musicButtons(c, v, 40)
        val sp = r.status?.optJSONObject("spotify")
        song(c, v, sp, compact = false)
        if (r.status == null) v.setTextViewText(R.id.artist, r.error ?: "")
        return v
    }

    private fun musicButtons(c: Context, v: RemoteViews, base: Int) {
        v.setOnClickPendingIntent(R.id.btn_prev, Refresh.button(c, "/api/spotify", "previous", base))
        v.setOnClickPendingIntent(R.id.btn_play, Refresh.button(c, "/api/spotify", "toggle", base + 1))
        v.setOnClickPendingIntent(R.id.btn_next, Refresh.button(c, "/api/spotify", "next", base + 2))
    }

    private fun song(c: Context, v: RemoteViews, sp: JSONObject?, compact: Boolean) {
        if (sp == null || sp.optString("title").isEmpty()) {
            v.setTextViewText(R.id.song, if (sp?.has("error") == true) "Spotify unavailable" else "Nothing playing")
            v.setTextViewText(R.id.artist, sp?.optString("error") ?: "Open Spotify on any device")
            v.setImageViewResource(R.id.art, R.drawable.ic_music)
            v.setImageViewResource(R.id.btn_play, R.drawable.ic_play_dark)
            if (!compact) {
                v.setProgressBar(R.id.progress, 1000, 0, false)
                v.setTextViewText(R.id.device, "")
            }
            return
        }
        val playing = sp.optBoolean("playing")
        v.setTextViewText(R.id.song, sp.optString("title"))
        v.setTextViewText(R.id.artist, sp.optString("artist"))
        v.setImageViewResource(R.id.btn_play, if (playing) R.drawable.ic_pause_dark else R.drawable.ic_play_dark)
        val art = art(c, sp.optString("art"), sp.optString("id"))
        if (art != null) v.setImageViewBitmap(R.id.art, art) else v.setImageViewResource(R.id.art, R.drawable.ic_music)
        if (!compact) {
            val dur = sp.optLong("duration_ms", 0)
            v.setProgressBar(R.id.progress, 1000, if (dur > 0) (1000 * sp.optLong("progress_ms") / dur).toInt() else 0, false)
            v.setTextViewText(R.id.device, (if (playing) "Playing on " else "Paused on ") + sp.optString("device"))
        }
    }

    /** Album art, downloaded once per track and kept small (widgets have a strict size limit for pictures). */
    private fun art(c: Context, url: String, id: String): Bitmap? {
        if (url.isEmpty()) return null
        val f = File(c.cacheDir, "art_${id.ifEmpty { url.hashCode().toString() }}.png")
        if (!f.exists()) {
            try {
                c.cacheDir.listFiles { x -> x.name.startsWith("art_") }?.sortedBy { it.lastModified() }
                    ?.dropLast(5)?.forEach { it.delete() }
                val raw = URL(url).openStream().use { it.readBytes() }
                val bmp = BitmapFactory.decodeByteArray(raw, 0, raw.size) ?: return null
                val small = Bitmap.createScaledBitmap(bmp, 200, 200 * bmp.height / maxOf(1, bmp.width), true)
                f.outputStream().use { small.compress(Bitmap.CompressFormat.PNG, 100, it) }
            } catch (_: Exception) {
                return null
            }
        }
        return BitmapFactory.decodeFile(f.path)
    }

    private fun clock(ms: Long): String = SimpleDateFormat("h:mm a", Locale.getDefault()).format(Date(ms))

    private fun speed(kbps: Int): String = if (kbps >= 1024) "%.1f MB/s".format(kbps / 1024.0) else "$kbps KB/s"
}
