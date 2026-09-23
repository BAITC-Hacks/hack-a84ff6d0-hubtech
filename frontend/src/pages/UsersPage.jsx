import { useEffect, useState } from 'react'
import { createUser, listUsers, updateUser } from '../api'
import { safeMessage } from '../format'
import { Loading, Status } from '../components/Common'

function UserCard({ account, currentUser, onUpdate, busy }) {
  const [roleEdit, setRoleEdit] = useState(null)
  const role = roleEdit ?? account.role
  const [password, setPassword] = useState('')
  const [resetOpen, setResetOpen] = useState(false)
  return (
    <article className={`user-card ${account.active ? '' : 'user-inactive'}`}>
      <div className="user-card-heading">
        <div>
          <h3>
            {account.username}
            {account.id === currentUser.id ? ' · вы' : ''}
          </h3>
          <span>{account.active ? 'Активна' : 'Отключена'}</span>
        </div>
        <button
          className="ghost"
          disabled={busy}
          onClick={() => onUpdate(account.id, { active: !account.active })}
        >
          {account.active ? 'Отключить' : 'Включить'}
        </button>
      </div>
      <form
        className="role-form"
        onSubmit={async (event) => {
          event.preventDefault()
          if (await onUpdate(account.id, { role })) setRoleEdit(null)
        }}
      >
        <label>
          Роль
          <select
            value={role}
            onChange={(event) => setRoleEdit(event.target.value)}
            disabled={busy}
          >
            <option value="manager">Менеджер</option>
            <option value="admin">Администратор</option>
          </select>
        </label>
        <button className="ghost" disabled={busy || role === account.role}>
          Сохранить роль
        </button>
      </form>
      <button
        className="link"
        aria-expanded={resetOpen}
        onClick={() => setResetOpen((value) => !value)}
      >
        Установить новый пароль
      </button>
      {resetOpen && (
        <form
          className="reset-password-form"
          onSubmit={async (event) => {
            event.preventDefault()
            if (await onUpdate(account.id, { password })) {
              setPassword('')
              setResetOpen(false)
            }
          }}
        >
          <label>
            Новый пароль
            <input
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={12}
              maxLength={256}
              required
              disabled={busy}
            />
          </label>
          <p className="help">
            От 12 символов. Активные сессии сотрудника завершатся.
          </p>
          <button className="primary" disabled={busy || password.length < 12}>
            Сменить пароль
          </button>
        </form>
      )}
    </article>
  )
}

export default function UsersPage({ user, active, onAuthRequired }) {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('manager')
  useEffect(() => {
    if (!active) return
    let current = true
    // Refresh the server-owned account list after a failed request or reauthentication.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError('')
    listUsers()
      .then((data) => {
        if (current) setUsers(data.items)
      })
      .catch((failure) => {
        if (!current) return
        if (failure.status === 401) onAuthRequired()
        else setError(safeMessage(failure))
      })
      .finally(() => {
        if (current) setLoading(false)
      })
    return () => {
      current = false
    }
  }, [active, attempt, onAuthRequired])
  async function update(id, changes) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const value = await updateUser(id, changes)
      setUsers((previous) =>
        previous.map((account) => (account.id === value.id ? value : account)),
      )
      setNotice('Учётная запись обновлена.')
      if (id === user.id) onAuthRequired()
      return true
    } catch (failure) {
      if (failure.status === 401) onAuthRequired()
      else setError(safeMessage(failure))
      return false
    } finally {
      setBusy(false)
    }
  }
  async function create(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const value = await createUser({
        username: username.trim(),
        password,
        role,
      })
      setUsers((previous) =>
        [...previous, value].sort((a, b) =>
          a.username.localeCompare(b.username),
        ),
      )
      setUsername('')
      setPassword('')
      setRole('manager')
      setNotice('Учётная запись создана.')
    } catch (failure) {
      if (failure.status === 401) onAuthRequired()
      else setError(safeMessage(failure))
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="step-label">АДМИНИСТРИРОВАНИЕ</span>
          <h1>Доступ команды</h1>
          <p>
            Менеджер работает со своими заказами. Администратор видит все заказы
            и управляет сотрудниками.
          </p>
        </div>
      </div>
      <Status
        error={error}
        success={notice}
        onRetry={
          error && !busy ? () => setAttempt((value) => value + 1) : undefined
        }
      />
      <section
        className="panel create-user"
        aria-labelledby="create-user-title"
      >
        <h2 id="create-user-title">Добавить сотрудника</h2>
        <form onSubmit={create}>
          <label>
            Имя пользователя
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              minLength={3}
              maxLength={80}
              pattern="[a-zA-Z0-9_.@\-]+"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck="false"
              required
              disabled={busy}
              aria-describedby="username-help"
            />
          </label>
          <label>
            Пароль
            <input
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={12}
              maxLength={256}
              required
              disabled={busy}
            />
          </label>
          <label>
            Роль
            <select
              value={role}
              onChange={(event) => setRole(event.target.value)}
              disabled={busy}
            >
              <option value="manager">Менеджер</option>
              <option value="admin">Администратор</option>
            </select>
          </label>
          <button
            className="primary"
            disabled={
              busy || username.trim().length < 3 || password.length < 12
            }
          >
            Создать учётную запись
          </button>
        </form>
        <p className="help" id="username-help">
          Логин: 3–80 латинских букв, цифры и символы _ . @ -. Пароль: от 12
          символов.
        </p>
      </section>
      {loading ? (
        <Loading>Загружаем сотрудников…</Loading>
      ) : (
        <section className="users-grid" aria-label="Сотрудники">
          {users.map((account) => (
            <UserCard
              key={account.id}
              account={account}
              currentUser={user}
              onUpdate={update}
              busy={busy}
            />
          ))}
        </section>
      )}
    </>
  )
}
