import { createClient } from '@supabase/supabase-js'

// Points at the selfie2id project, which is where the whaleradar_* tables
// live. The old project ("Grants") is paused, so every dashboard login was
// failing against a database that was not running.
const SUPABASE_URL = 'https://nnjhudtaegmeudrpusci.supabase.co'
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5uamh1ZHRhZWdtZXVkcnB1c2NpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM4NDA1OTksImV4cCI6MjA4OTQxNjU5OX0.cKH_EL_0RI6tVvOf_45vzeR1BlkGvHed-eY3ubcihpg'

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)

export async function checkProByToken(token) {
  // Goes through a definer function rather than reading the table directly.
  // whaleradar_subscribers has RLS on with no policies, so this anon key
  // cannot enumerate members even though it ships in the browser bundle. It
  // can only ask about a token the caller already holds.
  const { data, error } = await supabase.rpc('whaleradar_check_token', {
    p_token: token,
  })

  if (error) {
    console.error('token check failed:', error.message)
    return null
  }

  const user = Array.isArray(data) ? data[0] : data
  if (!user) return null

  // Check if expired
  if (user.is_pro && user.pro_expires) {
    const expires = new Date(user.pro_expires)
    if (new Date() > expires) return { ...user, is_pro: false }
  }

  return user
}
