package dev.batsy.widgets

import android.app.Activity
import android.os.Bundle
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import org.json.JSONObject

/** Pairing, a connection check, and a box to type to Batsy. The widgets do the rest. */
class MainActivity : Activity() {
    private lateinit var conn: TextView
    private lateinit var reply: TextView

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        if (intent.getBooleanExtra("preview", false)) return preview()
        setContentView(R.layout.activity_main)
        conn = findViewById(R.id.conn)
        reply = findViewById(R.id.reply)
        val ask = findViewById<EditText>(R.id.ask)
        val address = findViewById<EditText>(R.id.address)
        address.setText(Hub.remote(this))

        val send = {
            val text = ask.text.toString().trim()
            if (text.isNotEmpty()) {
                ask.setText("")
                show("…")
                background { Hub.post(this, "/api/batsy", JSONObject().put("action", "ask").put("text", text), 180_000) }
                    .then { show(it); Refresh.now(this) }
            }
        }
        findViewById<Button>(R.id.send).setOnClickListener { send() }
        ask.setOnEditorActionListener { _, id, _ -> if (id == EditorInfo.IME_ACTION_SEND) { send(); true } else false }

        findViewById<Button>(R.id.stop).setOnClickListener { button("/api/batsy", "stop") }
        findViewById<Button>(R.id.wake).setOnClickListener { button("/api/pc", "wake") }
        findViewById<Button>(R.id.refresh).setOnClickListener { check(); Refresh.now(this) }
        findViewById<Button>(R.id.pair).setOnClickListener {
            val code = findViewById<EditText>(R.id.code).text.toString().trim()
            val addr = address.text.toString()
            conn.text = "Pairing…"
            background { Hub.pair(this, addr, code) }.then { err ->
                if (err == null) { conn.text = "Paired."; Refresh.now(this); check() } else conn.text = err
            }
        }

        if (intent.getBooleanExtra("ask", false)) {  // the widget's Ask button: straight to typing
            ask.requestFocus()
            ask.post { getSystemService(InputMethodManager::class.java).showSoftInput(ask, 0) }
        }
    }

    override fun onResume() {
        super.onResume()
        if (::conn.isInitialized) check()
    }

    /** `am start -n dev.batsy.widgets/.MainActivity --ez preview true`: both widgets, drawn by the same code the
     *  home screen uses, with live data. For checking the design without placing widgets by hand. */
    private fun preview() {
        val d = resources.displayMetrics.density
        val col = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding((16 * d).toInt(), (40 * d).toInt(), (16 * d).toInt(), 0)
            setBackgroundColor(android.graphics.Color.parseColor("#3A4256"))
        }
        val dash = android.widget.FrameLayout(this)
        val music = android.widget.FrameLayout(this)
        col.addView(dash, android.widget.LinearLayout.LayoutParams(-1, (390 * d).toInt()))
        col.addView(music, android.widget.LinearLayout.LayoutParams(-1, (130 * d).toInt()).apply { topMargin = (16 * d).toInt() })
        setContentView(col)
        background { Hub.fetch(this) }.then { r ->
            dash.addView(Render.dashboard(this, r).apply(this, dash))
            music.addView(Render.music(this, r).apply(this, music))
        }
    }

    private fun check() {
        if (!Hub.paired(this)) {
            conn.text = "Not paired yet. Plug into the PC and say “Batsy, pair my phone”."
            return
        }
        conn.text = "Checking…"
        background { Hub.fetch(this) }.then { r ->
            conn.text = if (r.error == null) {
                val b = r.status?.optJSONObject("batsy")
                "Connected over ${r.route} · Batsy is ${b?.optString("state") ?: "idle"}"
            } else r.error
        }
    }

    private fun button(path: String, action: String) {
        show("…")
        background { Hub.post(this, path, JSONObject().put("action", action)) }.then { show(it); Refresh.now(this) }
    }

    private fun show(text: String) {
        reply.visibility = View.VISIBLE
        reply.text = text
    }

    /** Tiny helper: run on a background thread, then hand the result back to the UI thread. */
    private class Job<T>(val activity: Activity, val work: () -> T) {
        fun then(done: (T) -> Unit) {
            Thread {
                val result = work()
                activity.runOnUiThread { if (!activity.isFinishing) done(result) }
            }.start()
        }
    }

    private fun <T> background(work: () -> T) = Job(this, work)
}
