import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import { getOrder, saveOrder } from '../api'
import { safeMessage } from '../format'
import {
  draftFromOrder,
  hasSaveableChanges,
  invalidLines,
  rebaseDraft,
  saveableLines,
} from '../orderState'

export function useOrderEditor(orderId, active, onAuthRequired) {
  const [order, setOrder] = useState(null)
  const [draft, setDraft] = useState({ quantities: {}, approvals: {} })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(null)
  const [retry, setRetry] = useState(0)
  const state = useRef({
    order: null,
    draft,
    active,
    conflict: null,
    version: 0,
  })
  const flight = useRef(null)
  const mounted = useRef(true)
  const auth = useRef(onAuthRequired)
  useLayoutEffect(() => {
    auth.current = onAuthRequired
    state.current.active = active
  }, [active, onAuthRequired])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const setCurrentDraft = useCallback((next) => {
    state.current.draft = next
    state.current.version += 1
    setDraft(next)
  }, [])

  useEffect(() => {
    if (!active || state.current.order) return
    const controller = new AbortController()
    setLoading(true)
    setError('')
    getOrder(orderId, { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return
        const next = draftFromOrder(value)
        state.current = {
          ...state.current,
          order: value,
          draft: next,
          conflict: null,
        }
        setOrder(value)
        setDraft(next)
      })
      .catch((failure) => {
        if (controller.signal.aborted) return
        if (failure.status === 401) auth.current()
        else setError(safeMessage(failure))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [orderId, active, retry])

  const detectConflict = useCallback(async () => {
    const base = state.current.order
    if (!base) return
    const detected = { base, latest: null }
    state.current.conflict = detected
    setConflict(detected)
    try {
      const latest = await getOrder(base.id)
      if (!mounted.current) return
      const ready = { base, latest }
      state.current.conflict = ready
      setConflict(ready)
    } catch (failure) {
      if (failure.status === 401) auth.current()
      setError(safeMessage(failure))
    }
  }, [])

  const flush = useCallback(
    async function flushPending() {
      if (flight.current) {
        const finished = await flight.current
        if (!finished) return null
        if (state.current.conflict || !state.current.active) return null
        return flushPending()
      }
      if (
        !state.current.order ||
        !state.current.active ||
        state.current.conflict
      )
        return null
      if (!hasSaveableChanges(state.current.order, state.current.draft))
        return state.current.order

      const work = async () => {
        setSaving(true)
        setError('')
        try {
          while (
            state.current.active &&
            !state.current.conflict &&
            hasSaveableChanges(state.current.order, state.current.draft)
          ) {
            const base = state.current.order
            const requestedDraft = state.current.draft
            const version = state.current.version
            let saved
            try {
              saved = await saveOrder(
                base.id,
                base.revision,
                saveableLines(base, requestedDraft),
              )
            } catch (failure) {
              if (!mounted.current) return null
              if (failure.status === 401) {
                state.current.active = false
                auth.current()
                return null
              }
              if (failure.status === 409) {
                await detectConflict()
                return null
              }
              setError(safeMessage(failure))
              return null
            }
            if (!mounted.current) return null
            state.current.order = saved
            setOrder(saved)
            if (state.current.version === version) {
              const normalized = draftFromOrder(saved)
              for (const line of invalidLines(base, requestedDraft)) {
                normalized.quantities[line.line_id] =
                  requestedDraft.quantities[line.line_id]
                normalized.approvals[line.line_id] = false
              }
              state.current.draft = normalized
              setDraft(normalized)
            }
          }
          return state.current.order
        } finally {
          if (mounted.current) setSaving(false)
        }
      }
      const pending = work()
      flight.current = pending
      try {
        return await pending
      } finally {
        flight.current = null
      }
    },
    [detectConflict],
  )

  const dirty = Boolean(order && hasSaveableChanges(order, draft))
  const invalid = order ? invalidLines(order, draft) : []
  useEffect(() => {
    if (!active || !dirty || conflict || error) return
    const timer = setTimeout(() => {
      void flush()
    }, 1000)
    return () => clearTimeout(timer)
  }, [draft, active, dirty, conflict, error, flush])

  useEffect(() => {
    if (!dirty && !invalid.length) return
    const preventLoss = (event) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', preventLoss)
    return () => window.removeEventListener('beforeunload', preventLoss)
  }, [dirty, invalid.length])

  const edit = useCallback(
    (lineId, quantity) => {
      const current = state.current.draft
      setCurrentDraft({
        quantities: { ...current.quantities, [lineId]: quantity },
        approvals: { ...current.approvals, [lineId]: false },
      })
      setError('')
    },
    [setCurrentDraft],
  )

  const approve = useCallback(
    async (lineIds, value) => {
      if (!state.current.active || state.current.conflict) return false
      // Persist quantity first: a quantity change always revokes approval on the server.
      if (value && !(await flush())) return false
      const current = state.current.draft
      const next = { ...current.approvals }
      for (const id of lineIds) next[id] = value
      setCurrentDraft({ ...current, approvals: next })
      return Boolean(await flush())
    },
    [flush, setCurrentDraft],
  )

  const resolveConflict = useCallback(
    (keepLocal) => {
      const current = state.current
      if (!current.conflict?.latest) return
      const latest = current.conflict.latest
      const next = keepLocal
        ? rebaseDraft(current.conflict.base, latest, current.draft)
        : draftFromOrder(latest)
      state.current.order = latest
      state.current.conflict = null
      setOrder(latest)
      setConflict(null)
      setCurrentDraft(next)
      setError('')
    },
    [setCurrentDraft],
  )

  const refreshConflict = useCallback(async () => {
    try {
      const latest = await getOrder(orderId)
      const next = { ...state.current.conflict, latest }
      state.current.conflict = next
      setConflict(next)
      setError('')
    } catch (failure) {
      if (failure.status === 401) auth.current()
      setError(safeMessage(failure))
    }
  }, [orderId])

  return {
    order,
    draft,
    loading,
    saving,
    dirty,
    invalid,
    error,
    conflict,
    edit,
    approve,
    flush,
    resolveConflict,
    refreshConflict,
    detectConflict,
    retry: () => {
      setError('')
      setRetry((value) => value + 1)
      void flush()
    },
    replaceOrder: (next) => {
      state.current.order = next
      setOrder(next)
    },
  }
}
