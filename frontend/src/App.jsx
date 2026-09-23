import { useCallback, useEffect, useRef, useState } from 'react'
import { getSession, logout } from './api'
import { safeMessage } from './format'
import { Empty, Loading, Status } from './components/Common'
import CalculatePage from './pages/CalculatePage'
import HistoryPage from './pages/HistoryPage'
import LoginPage from './pages/LoginPage'
import OrderPage from './pages/OrderPage'
import UsersPage from './pages/UsersPage'
import Icon from './Icons'
import './App.css'

function routeFromHash(hash) {
  try {
    const [path, query] = (hash || '#/orders').slice(1).split('?')
    if (path === '/orders') return { page: 'orders' }
    if (path.startsWith('/orders/') && path.slice(8))
      return { page: 'order', id: decodeURIComponent(path.slice(8)) }
    if (path === '/calculate')
      return {
        page: 'calculate',
        jobId: new URLSearchParams(query).get('job'),
      }
    if (path === '/users') return { page: 'users' }
    if (path === '/account') return { page: 'account' }
    return { page: 'missing' }
  } catch {
    return { page: 'missing' }
  }
}

function Workspace({ session, active, onAuthRequired, onLogout }) {
  const user = session.user
  const [hash, setHash] = useState(window.location.hash || '#/orders')
  const [logoutBusy, setLogoutBusy] = useState(false)
  const [logoutError, setLogoutError] = useState('')
  const currentHash = useRef(hash)
  const guard = useRef(null)
  const navigating = useRef(false)
  const route = routeFromHash(hash)
  const registerGuard = useCallback((fn) => {
    guard.current = fn
    return () => {
      if (guard.current === fn) guard.current = null
    }
  }, [])

  const navigate = useCallback(async (target, { skipGuard = false } = {}) => {
    if (target === currentHash.current || navigating.current) return
    navigating.current = true
    try {
      if (!skipGuard && guard.current && !(await guard.current())) return
      currentHash.current = target
      window.history.pushState(null, '', target)
      setHash(target)
      window.scrollTo({ top: 0, behavior: 'instant' })
    } finally {
      navigating.current = false
    }
  }, [])

  useEffect(() => {
    const change = async () => {
      const target = window.location.hash || '#/orders'
      if (target === currentHash.current) return
      if (guard.current && !(await guard.current())) {
        window.history.replaceState(null, '', currentHash.current)
        return
      }
      currentHash.current = target
      setHash(target)
    }
    window.addEventListener('hashchange', change)
    return () => window.removeEventListener('hashchange', change)
  }, [])

  async function signOut() {
    if (guard.current && !(await guard.current())) return
    setLogoutBusy(true)
    setLogoutError('')
    try {
      await logout()
      onLogout()
    } catch (failure) {
      if (failure.status === 401) onLogout()
      else setLogoutError(safeMessage(failure))
    } finally {
      setLogoutBusy(false)
    }
  }

  const shared = { active, onAuthRequired, navigate }
  const nav = [
    ['#/orders', 'orders', 'Заказы'],
    ['#/calculate', 'calculate', 'Рассчитать'],
    ...(user.role === 'admin' ? [['#/users', 'users', 'Команда']] : []),
  ]
  return (
    <div className="app" hidden={!active}>
      <a
        className="skip-link"
        href="#main"
        onClick={(event) => {
          event.preventDefault()
          document.getElementById('main')?.focus()
        }}
      >
        Перейти к содержимому
      </a>
      <header className="topbar">
        <a
          className="brand"
          href="#/orders"
          aria-label="Umytpa — заказы"
          onClick={(event) => {
            event.preventDefault()
            navigate('#/orders')
          }}
        >
          <span className="brand-mark">
            <Icon name="box" size={26} />
          </span>
          umytpa<span className="brand-dot">.</span>
        </a>
        <nav className="main-nav" aria-label="Основная навигация">
          {nav.map(([target, page, label]) => (
            <a
              key={target}
              href={target}
              className={
                route.page === page ||
                (page === 'orders' && route.page === 'order')
                  ? 'nav-active'
                  : ''
              }
              aria-current={
                route.page === page ||
                (page === 'orders' && route.page === 'order')
                  ? 'page'
                  : undefined
              }
              onClick={(event) => {
                event.preventDefault()
                navigate(target)
              }}
            >
              {label}
            </a>
          ))}
        </nav>
        <a
          href="#/account"
          className="account-link"
          aria-label={'Учётная запись ' + user.username}
          onClick={(event) => {
            event.preventDefault()
            navigate('#/account')
          }}
        >
          <span className="avatar">
            {user.username.slice(0, 1).toUpperCase()}
          </span>
          <span>{user.username}</span>
        </a>
      </header>
      <main id="main" tabIndex="-1">
        {route.page === 'orders' && <HistoryPage {...shared} user={user} />}
        {route.page === 'calculate' && (
          <CalculatePage {...shared} jobId={route.jobId} />
        )}
        {route.page === 'order' && (
          <OrderPage
            key={route.id}
            {...shared}
            orderId={route.id}
            registerGuard={registerGuard}
          />
        )}
        {route.page === 'users' &&
          (user.role === 'admin' ? (
            <UsersPage {...shared} user={user} />
          ) : (
            <Empty title="Недостаточно прав">
              Раздел сотрудников доступен администратору.
            </Empty>
          ))}
        {route.page === 'account' && (
          <>
            <div className="page-heading">
              <div>
                <span className="step-label">УЧЁТНАЯ ЗАПИСЬ</span>
                <h1>Ваш рабочий доступ</h1>
              </div>
            </div>
            <section
              className="panel account-panel"
              aria-label="Данные учётной записи"
            >
              <dl>
                <div>
                  <dt>Имя пользователя</dt>
                  <dd>{user.username}</dd>
                </div>
                <div>
                  <dt>Роль</dt>
                  <dd>
                    {user.role === 'admin'
                      ? 'Администратор'
                      : 'Менеджер закупок'}
                  </dd>
                </div>
                <div>
                  <dt>Статус</dt>
                  <dd>{user.active ? 'Активна' : 'Отключена'}</dd>
                </div>
              </dl>
              <p>Для смены пароля обратитесь к администратору организации.</p>
              <Status error={logoutError} />
              <button className="ghost" onClick={signOut} disabled={logoutBusy}>
                {logoutBusy ? 'Выходим…' : 'Выйти из учётной записи'}
              </button>
            </section>
          </>
        )}
        {route.page === 'missing' && (
          <>
            <Empty title="Страница не найдена">
              Проверьте адрес или вернитесь к заказам.
            </Empty>
            <button className="primary" onClick={() => navigate('#/orders')}>
              К заказам
            </button>
          </>
        )}
      </main>
      <footer className="footer">
        <span className="footer-brand">umytpa.</span>
        <span>Осознанные закупки · ТОО «Электрокомплект»</span>
        <span>Решение остаётся за вами</span>
      </footer>
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState(null)
  const [retainedSession, setRetainedSession] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const onAuthRequired = useCallback(() => setSession(null), [])
  useEffect(() => {
    const controller = new AbortController()
    // Remote session retries must replace the previous failure with a loading state.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError('')
    getSession({ signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) {
          setSession(value)
          setRetainedSession(value)
        }
      })
      .catch((failure) => {
        if (controller.signal.aborted) return
        if (failure.status !== 401) setError(safeMessage(failure))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [attempt])
  function onLogin(value) {
    if (retainedSession && retainedSession.user.id !== value.user.id)
      window.history.replaceState(null, '', '#/orders')
    setSession(value)
    setRetainedSession(value)
  }
  if (loading)
    return (
      <main className="session-loading">
        <Loading>Проверяем доступ…</Loading>
      </main>
    )
  if (error)
    return (
      <main className="session-loading">
        <h1>Не удалось подключиться</h1>
        <Status
          error={error}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      </main>
    )
  return (
    <>
      {retainedSession && (
        <Workspace
          key={`workspace:${retainedSession.user.id}`}
          session={session || retainedSession}
          active={Boolean(session)}
          onAuthRequired={onAuthRequired}
          onLogout={() => {
            setSession(null)
            setRetainedSession(null)
            window.history.replaceState(null, '', '#/orders')
          }}
        />
      )}
      {!session && (
        <LoginPage
          key={`login:${retainedSession?.user.id || 'initial'}`}
          onLogin={onLogin}
          expiredUser={retainedSession?.user}
        />
      )}
    </>
  )
}
