import { Component } from 'react'
import Icon from '../Icons'

export function Status({ error, success, children, onRetry }) {
  if (!error && !success && !children) return null
  return (
    <div
      className={`feedback ${error ? 'feedback-error' : success ? 'feedback-success' : ''}`}
      role={error ? 'alert' : 'status'}
    >
      <Icon name={error ? 'alert' : success ? 'check' : 'clock'} size={20} />
      <span>{error || success || children}</span>
      {onRetry && (
        <button className="ghost" onClick={onRetry}>
          Повторить
        </button>
      )}
    </div>
  )
}

export function Loading({ children = 'Загружаем…' }) {
  return (
    <p className="loading-status" role="status">
      <span className="spinner" />
      {children}
    </p>
  )
}

export function Empty({ title, children }) {
  return (
    <div className="empty">
      <Icon name="box" size={32} />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  )
}

export class ErrorBoundary extends Component {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  render() {
    if (this.state.failed)
      return (
        <main className="fatal-screen">
          <h1>Не удалось показать страницу</h1>
          <p>
            Обновите страницу. Сохранённые на сервере заказы останутся в
            истории.
          </p>
          <button className="primary" onClick={() => window.location.reload()}>
            Обновить страницу
          </button>
        </main>
      )
    return this.props.children
  }
}
