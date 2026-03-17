import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL = 'https://zeyqrpfwcvhtzpwjvpfg.supabase.co'
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpleXFycGZ3Y3ZodHpwd2p2cGZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzAyNzUyNTYsImV4cCI6MjA4NTg1MTI1Nn0.BNBhmZMbxgLP8uKfW86ZY5gv_2ZBPXAZQITVAv_NqDg'

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)

export async function checkProByToken(token) {
  const { data, error } = await supabase
    .from('subscribers')
    .select('chat_id, username, is_pro, pro_expires')
    .eq('auth_token', token)
    .single()

  if (error || !data) return null

  // Check if expired
  if (data.is_pro && data.pro_expires) {
    const expires = new Date(data.pro_expires)
    if (new Date() > expires) return { ...data, is_pro: false }
  }

  return data
}
