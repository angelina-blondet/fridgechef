import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = "https://wspxtlusvqhkunchrmou.supabase.co";
const SUPABASE_KEY = "sb_publishable_4bPYOWBqhiWVQ-sZTHGhFA_IMHpBajO";

export const supabase = createClient(
    SUPABASE_URL,
    SUPABASE_KEY,
    {
        auth: {
            persistSession: true,
            autoRefreshToken: true,
            detectSessionInUrl: true
        }
    }
);
