package net.sentinel.live;

import android.content.Context;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Small view helpers, matching the build's no-dependency, no-lambda style. */
final class Ui {

    static final int BG = 0xFF080B10;
    static final int SURFACE = 0xFF0E131A;
    static final int LINE = 0xFF1F2937;
    static final int TEXT = 0xFFE6EDF6;
    static final int MUTED = 0xFF8494A8;
    static final int ACCENT = 0xFF34D399;

    private Ui() {
    }

    static int dp(Context c, int value) {
        return (int) TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP, value, c.getResources().getDisplayMetrics());
    }

    static LinearLayout column(Context c) {
        LinearLayout l = new LinearLayout(c);
        l.setOrientation(LinearLayout.VERTICAL);
        return l;
    }

    static TextView text(Context c, String value, int sp) {
        TextView t = new TextView(c);
        t.setText(value);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(TEXT);
        return t;
    }

    static TextView mono(Context c, String value, int sp) {
        TextView t = text(c, value, sp);
        t.setTypeface(android.graphics.Typeface.MONOSPACE);
        return t;
    }

    static Button button(Context c, String label, boolean primary) {
        Button b = new Button(c);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        b.setTextColor(primary ? 0xFF04140E : TEXT);
        b.setBackgroundColor(primary ? ACCENT : SURFACE);
        b.setPadding(dp(c, 14), dp(c, 10), dp(c, 14), dp(c, 10));
        return b;
    }

    static View spacer(Context c, int heightDp) {
        View v = new View(c);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(c, heightDp));
        v.setLayoutParams(lp);
        return v;
    }

    static LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    static LinearLayout.LayoutParams center() {
        LinearLayout.LayoutParams lp = matchWrap();
        lp.gravity = Gravity.CENTER;
        return lp;
    }
}