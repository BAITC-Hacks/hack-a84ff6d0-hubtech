import { useState } from 'react'
import { login } from '../api'
import { safeMessage } from '../format'
import { Status } from '../components/Common'
import Icon from '../Icons'

export default function LoginPage({ onLogin, expiredUser }) {
  const [username, setUsername] = useState(expiredUser?.username || '')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const session = await login(username.trim(), password)
      setPassword('')
      onLogin(session)
    } catch (failure) {
      setError(safeMessage(failure))
      setPassword('')
    } finally {
      setBusy(false)
    }
  }
  return (
    <main className="login-shell">
      <div className="login-brand">
        <span className="brand-mark">
          <Icon name="box" size={28} />
        </span>
        umytpa.
      </div>
      <div className="login-intro">
        <span className="step-label">ЭКТ · ЗАКУПКИ</span>
        <h1>
          Уверенность
          <br />в каждом заказе.
        </h1>
        <p>Прогноз спроса, прозрачный расчёт и решения вашей команды.</p>
      </div>
      <section className="login-card" aria-labelledby="login-title">
        <span className="step-label">РАБОЧЕЕ ПРОСТРАНСТВО</span>
        <h2 id="login-title">
          {expiredUser ? 'Продолжить работу' : 'Вход для сотрудников'}
        </h2>
        {expiredUser && (
          <p className="reauth-notice">
            Сессия завершена. Правки сохранены на этой странице. Войдите как{' '}
            <strong>{expiredUser.username}</strong>, чтобы продолжить с ними.
            Вход под другой учётной записью откроет её рабочее пространство.
          </p>
        )}
        <form onSubmit={submit}>
          <label>
            Имя пользователя
            <input
              autoComplete="username"
              autoCapitalize="none"
              spellCheck="false"
              name="username"
              maxLength={80}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
              disabled={busy}
              autoFocus
            />
          </label>
          <label>
            Пароль
            <input
              type="password"
              autoComplete="current-password"
              name="password"
              maxLength={256}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              disabled={busy}
            />
          </label>
          <Status error={error} />
          <button
            className="primary"
            disabled={busy || !username.trim() || !password}
          >
            {busy ? (
              <>
                <span className="spinner" />
                Входим…
              </>
            ) : (
              <>
                Войти
                <Icon name="arrow" />
              </>
            )}
          </button>
        </form>
        <p className="help">
          Учётную запись и восстановление доступа предоставляет администратор
          вашей организации.
        </p>
      </section>
      <p className="login-footnote">
        Сервис рекомендованных заказов · ТОО «Электрокомплект»
      </p>
    </main>
  )
}
