package demo;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Synthetic MCTL-owned fixture for spike #796. No auth, licence, DRM or anti-tamper logic. */
public final class Flags {
    private Flags() {}

    private static Map<String, Boolean> layer(String... kv) {
        Map<String, Boolean> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put(kv[i], Boolean.parseBoolean(kv[i + 1]));
        return m;
    }

    /** Hidden behaviour: resolution order is override, then env, then built-in default. */
    static boolean resolve(String flag, Map<String, Boolean> override, Map<String, Boolean> env) {
        Map<String, Boolean> defaults = layer("beta", "false", "compact", "true");
        for (Map<String, Boolean> m : List.of(override, env, defaults)) {
            Boolean v = m.get(flag);
            if (v != null) return v;
        }
        return false;
    }

    public static void main(String[] args) {
        Map<String, Boolean> override = layer();
        Map<String, Boolean> env = layer();
        for (String a : args) {
            String[] p = a.split("=", 2);
            if (p.length < 2) continue;
            if (p[0].startsWith("o:")) override.put(p[0].substring(2), Boolean.parseBoolean(p[1]));
            else if (p[0].startsWith("e:")) env.put(p[0].substring(2), Boolean.parseBoolean(p[1]));
        }
        System.out.println("beta=" + resolve("beta", override, env));
        System.out.println("compact=" + resolve("compact", override, env));
    }
}
