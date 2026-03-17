import { useEffect, useRef } from 'react'

export default function TelegramLogin({ onAuth }) {
  const containerRef = useRef(null)

  useEffect(() => {
    if (!containerRef.current) return

    // Check if user is already logged in
    const saved = localStorage.getItem('tg_user')
    if (saved) {
      try {
        const user = JSON.parse(saved)
        onAuth(user)
        return
      } catch { /* ignore */ }
    }

    // Create the Telegram Login Widget
    const script = document.createElement('script')
    script.src = 'https://telegram.org/js/telegram-widget.js?22'
    script.setAttribute('data-telegram-login', 'Whaleradarbot_bot')
    script.setAttribute('data-size', 'large')
    script.setAttribute('data-radius', '0')
    script.setAttribute('data-onauth', '__onTelegramAuth(user)')
    script.setAttribute('data-request-access', 'write')
    script.async = true

    // Global callback for Telegram
    window.__onTelegramAuth = (user) => {
      localStorage.setItem('tg_user', JSON.stringify(user))
      onAuth(user)
    }

    containerRef.current.innerHTML = ''
    containerRef.current.appendChild(script)

    return () => {
      delete window.__onTelegramAuth
    }
  }, [onAuth])

  return <div ref={containerRef} />
}
