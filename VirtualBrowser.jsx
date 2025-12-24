import React, { useState, useEffect, useCallback, useRef } from 'react';
import { 
  PanelRightClose, PanelRightOpen, Puzzle, Settings, 
  Maximize2, Minimize2, X, GripVertical, Monitor
} from 'lucide-react';
import { cn } from '../../lib/utils';
import TabBar from './TabBar';
import AddressBar from './AddressBar';
import BrowserViewport from './BrowserViewport';
import ExtensionsPanel from './ExtensionsPanel';
import { mockTabs, browserSettings } from '../../data/mock';
import { browserApi, extensionsApi, searchApi } from '../../services/browserApi';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '../ui/tooltip';

const VirtualBrowser = ({ defaultExpanded = true, onClose }) => {
  // Browser state
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showExtensions, setShowExtensions] = useState(false);
  
  // Tabs state
  const [tabs, setTabs] = useState(mockTabs);
  const [activeTabId, setActiveTabId] = useState(mockTabs[0]?.id);
  
  // Browser session state
  const [sessionId, setSessionId] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [screenshot, setScreenshot] = useState(null);
  const [error, setError] = useState(null);
  const [canGoBack, setCanGoBack] = useState(false);
  const [canGoForward, setCanGoForward] = useState(false);
  const [sessionInitializing, setSessionInitializing] = useState(false);
  
  // WebSocket state
  const wsRef = useRef(null);
  const [wsConnected, setWsConnected] = useState(false);
  
  // Extensions state
  const [extensions, setExtensions] = useState([]);
  const [developerMode, setDeveloperMode] = useState(browserSettings.developerMode);
  
  // Search suggestions state
  const [searchSuggestions, setSearchSuggestions] = useState([]);
  
  // Resize state
  const [browserWidth, setBrowserWidth] = useState(1200);
  const resizeRef = useRef(null);
  const [isResizing, setIsResizing] = useState(false);

  const activeTab = tabs.find(t => t.id === activeTabId);

// Backend uses UUIDs for tab/session IDs; frontend just displays ids.
  const generateTabId = () => `tab-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

// Initialize browser session
  const initSession = useCallback(async () => {
    if (sessionId || sessionInitializing) return;

    setSessionInitializing(true);
    try {
      const session = await browserApi.createSession();
      // Backend now returns initial_tab_id for real multi-tab contexts
      setSessionId(session.session_id);
      setActiveTabId(session.initial_tab_id || mockTabs[0]?.id);
      // Ensure local tabs list is aligned with backend
      if (session.initial_tab_id) {
        setTabs([
          {
            id: session.initial_tab_id,
            title: 'New Tab',
            url: 'chrome://newtab',
            favicon: null,
            isActive: true,
            isLoading: false,
          },
        ]);
      }
      console.log('Browser session created:', session.session_id);
    } catch (err) {
      console.error('Failed to create browser session:', err);
      setError('Failed to initialize browser session');
    } finally {
      setSessionInitializing(false);
    }
  }, [sessionId, sessionInitializing]);

// Connect WebSocket when session is ready
  useEffect(() => {
    if (!sessionId || wsRef.current) return;

    const ws = browserApi.createWebSocket(sessionId);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log('WebSocket connected');
      setWsConnected(true);

      // Ensure backend active tab matches current activeTabId
      if (activeTabId) {
        ws.send(JSON.stringify({ type: 'activate_tab', tabId: activeTabId }));
      }
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        if (data.type === 'screenshot') {
          // Only apply screenshot/title to matching tab
          if (data.tab_id && data.tab_id !== activeTabId) {
            // Update background tab metadata only
            setTabs(prev => prev.map(t =>
              t.id === data.tab_id
                ? { ...t, url: data.url || t.url, title: data.title || t.title, isLoading: false }
                : t
            ));
            return;
          }

          setScreenshot(data.data);
          setIsLoading(false);
          

          if (data.url && data.title) {
            setTabs(prev => prev.map(t =>
              t.id === activeTabId
                ? { ...t, url: data.url, title: data.title || data.url, isLoading: false }
                : t
            ));
          }
        } else if (data.type === 'state') {
          if (data.state === 'navigating') {
            setIsLoading(true);
          }
          if (data.state === 'idle') {
            setIsLoading(false);
          }
        } else if (data.type === 'tab_created') {
          const newId = data.tab_id;
          if (newId) {
            setTabs(prev => [
              ...prev.map(t => ({ ...t, isActive: false })),
              {
                id: newId,
                title: 'New Tab',
                url: 'chrome://newtab',
                favicon: null,
                isActive: true,
                isLoading: false,
              },
            ]);
            setActiveTabId(newId);
            setScreenshot(null);
          }
        } else if (data.type === 'tab_closed') {
          // server will send active_tab_id
          const closedId = data.tab_id;
          const newActive = data.active_tab_id;
          setTabs(prev => {
            const remaining = prev.filter(t => t.id !== closedId);
            if (remaining.length === 0) return prev;
            return remaining.map(t => ({ ...t, isActive: t.id === newActive }));
          });
          if (newActive) {
            setActiveTabId(newActive);
          }
        } else if (data.type === 'error') {
          setError(data.message);
          setIsLoading(false);
        }
      } catch (err) {
        console.error('WebSocket message parse error:', err);
      }
    };
    
    ws.onclose = () => {
      console.log('WebSocket disconnected');
      setWsConnected(false);
      wsRef.current = null;
    };

    ws.onerror = (err) => {
      console.error('WebSocket error:', err);
      setWsConnected(false);
    };

    return () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.close();
      }
    };
  }, [sessionId, activeTabId]);

  // Initialize session on mount
  useEffect(() => {
    initSession();
    return () => {
      // Cleanup session on unmount
      if (sessionId) {
        browserApi.closeSession(sessionId).catch(console.error);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  // Fetch extensions on mount
  useEffect(() => {
    const fetchExtensions = async () => {
      try {
        const exts = await extensionsApi.listExtensions();
        setExtensions(exts);
      } catch (err) {
        console.error('Failed to fetch extensions:', err);
      }
    };
    fetchExtensions();
  }, []);

  // Update session status periodically
  useEffect(() => {
    if (!sessionId) return;

    const updateStatus = async () => {
      try {
        const status = await browserApi.getSessionStatus(sessionId);
        setCanGoBack(status.can_go_back);
        setCanGoForward(status.can_go_forward);
      } catch (err) {
        // Session might have expired
        console.error('Failed to get session status:', err);
      }
    };

    const interval = setInterval(updateStatus, 5000);
    return () => clearInterval(interval);
  }, [sessionId]);

  // Tab management
const handleNewTab = useCallback(async () => {
    // Prefer real backend tab creation
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'new_tab' }));
      return;
    }

    if (sessionId) {
      try {
        const created = await browserApi.createTab(sessionId);
        const newId = created.tab_id;
        const newTab = {
          id: newId,
          title: 'New Tab',
          url: 'chrome://newtab',
          favicon: null,
          isActive: true,
          isLoading: false,
        };
        setTabs(prev => [...prev.map(t => ({ ...t, isActive: false })), newTab]);
        setActiveTabId(newId);
        setScreenshot(null);
        setError(null);
        return;
      } catch (e) {
        // fall back to UI-only tab if backend fails
      }
    }

    const newTab = {
      id: generateTabId(),
      title: 'New Tab',
      url: 'chrome://newtab',
      favicon: null,
      isActive: true,
      isLoading: false
    };
    setTabs(prev => [...prev.map(t => ({ ...t, isActive: false })), newTab]);
    setActiveTabId(newTab.id);
    setScreenshot(null);
    setError(null);
  }, []);

  const handleTabClose = useCallback((tabId) => {
    setTabs(prev => {
      const newTabs = prev.filter(t => t.id !== tabId);
      if (newTabs.length === 0) {
        const newTab = {
          id: generateTabId(),
          title: 'New Tab',
          url: 'chrome://newtab',
          favicon: null,
          isActive: true,
          isLoading: false
        };
        setActiveTabId(newTab.id);
        return [newTab];
      }
      if (tabId === activeTabId) {
        const closedIndex = prev.findIndex(t => t.id === tabId);
        const newActiveTab = newTabs[Math.min(closedIndex, newTabs.length - 1)];
        setActiveTabId(newActiveTab.id);
      }
      return newTabs;
    });
  }, [activeTabId]);

  const handleTabActivate = useCallback((tabId) => {
    setActiveTabId(tabId);
    setTabs(prev => prev.map(t => ({ ...t, isActive: t.id === tabId })));
  }, []);

  const handleTabsReorder = useCallback((draggedId, targetId) => {
    setTabs(prev => {
      const newTabs = [...prev];
      const draggedIndex = newTabs.findIndex(t => t.id === draggedId);
      const targetIndex = newTabs.findIndex(t => t.id === targetId);
      const [draggedTab] = newTabs.splice(draggedIndex, 1);
      newTabs.splice(targetIndex, 0, draggedTab);
      return newTabs;
    });
  }, []);

  // Navigation
  const handleNavigate = useCallback(async (url) => {
    if (!url || url === 'chrome://newtab') {
      setScreenshot(null);
      setTabs(prev => prev.map(t => 
        t.id === activeTabId 
          ? { ...t, url: 'chrome://newtab', title: 'New Tab', isLoading: false }
          : t
      ));
      return;
    }
    
    setIsLoading(true);
    setError(null);
    
    // Update tab to show loading
    setTabs(prev => prev.map(t => 
      t.id === activeTabId 
        ? { ...t, url, title: 'Loading...', isLoading: true }
        : t
    ));

    // If WebSocket is connected, use it for navigation
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'navigate', url }));
    } else if (sessionId) {
      // Fallback to REST API
      try {
        const result = await browserApi.navigate(sessionId, url);
        setTabs(prev => prev.map(t => 
          t.id === activeTabId 
            ? { ...t, url: result.url, title: result.title || url, isLoading: false }
            : t
        ));
        
        // Get screenshot
        const screenshotData = await browserApi.getScreenshot(sessionId);
        setScreenshot(screenshotData.screenshot);
      } catch (err) {
        setError(err.message || 'Failed to load page');
        setTabs(prev => prev.map(t => 
          t.id === activeTabId ? { ...t, isLoading: false } : t
        ));
      } finally {
        setIsLoading(false);
      }
    }
  }, [activeTabId, sessionId]);

  const handleBack = useCallback(async () => {
    if (!canGoBack) return;
    
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'back' }));
    } else if (sessionId) {
      await browserApi.goBack(sessionId);
    }
  }, [canGoBack, sessionId]);

  const handleForward = useCallback(async () => {
    if (!canGoForward) return;
    
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'forward' }));
    } else if (sessionId) {
      await browserApi.goForward(sessionId);
    }
  }, [canGoForward, sessionId]);

  const handleRefresh = useCallback(async () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'refresh' }));
    } else if (sessionId) {
      setIsLoading(true);
      await browserApi.refresh(sessionId);
      const screenshotData = await browserApi.getScreenshot(sessionId);
      setScreenshot(screenshotData.screenshot);
      setIsLoading(false);
    }
  }, [sessionId]);

  const handleStop = useCallback(() => {
    setIsLoading(false);
    setTabs(prev => prev.map(t => 
      t.id === activeTabId ? { ...t, isLoading: false } : t
    ));
  }, [activeTabId]);

  const handleHome = useCallback(() => {
    handleNavigate('chrome://newtab');
  }, [handleNavigate]);

  const handleBookmark = useCallback(() => {
    console.log('Bookmark:', activeTab?.url);
  }, [activeTab]);

  // Search suggestions with debounce
  const searchTimeoutRef = useRef(null);
  const handleSearchInput = useCallback(async (query) => {
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }
    
    if (!query || query.length < 2) {
      setSearchSuggestions([]);
      return;
    }
    
    searchTimeoutRef.current = setTimeout(async () => {
      try {
        const result = await searchApi.getSuggestions(query, 5);
        setSearchSuggestions(result.suggestions || []);
      } catch (err) {
        console.error('Failed to get search suggestions:', err);
      }
    }, 300);
  }, []);

  // Extensions management
  const handleToggleExtension = useCallback(async (extId) => {
    const ext = extensions.find(e => e.id === extId);
    if (!ext) return;
    
    try {
      const updated = await extensionsApi.toggleExtension(extId, !ext.enabled);
      setExtensions(prev => prev.map(e => e.id === extId ? updated : e));
    } catch (err) {
      console.error('Failed to toggle extension:', err);
    }
  }, [extensions]);

  const handleRemoveExtension = useCallback(async (extId) => {
    try {
      await extensionsApi.removeExtension(extId);
      setExtensions(prev => prev.filter(e => e.id !== extId));
    } catch (err) {
      console.error('Failed to remove extension:', err);
    }
  }, []);

  const handleLoadUnpacked = useCallback(async (path) => {
    try {
      const newExt = await extensionsApi.loadUnpacked(path);
      setExtensions(prev => [...prev, newExt]);
    } catch (err) {
      console.error('Failed to load unpacked extension:', err);
    }
  }, []);

  const handlePackExtension = useCallback(async (path, keyPath) => {
    try {
      await extensionsApi.packExtension(path, keyPath);
      console.log('Extension packed successfully');
    } catch (err) {
      console.error('Failed to pack extension:', err);
    }
  }, []);

  const handleRefreshExtensions = useCallback(async () => {
    try {
      const exts = await extensionsApi.listExtensions();
      setExtensions(exts);
    } catch (err) {
      console.error('Failed to refresh extensions:', err);
    }
  }, []);

  // Viewport interactions
  const handleMouseMove = useCallback((x, y) => {
    // Mouse move is handled by WebSocket in viewport
  }, []);

  const handleMouseClick = useCallback((x, y, button, clickCount = 1) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ 
        type: 'click', 
        x, 
        y, 
        button: button === 0 ? 'left' : button === 2 ? 'right' : 'middle',
        clickCount 
      }));
    }
  }, []);

  const handleKeyPress = useCallback((key, keyCode, modifiers) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'keypress', key, modifiers }));
    }
  }, []);

  const handleType = useCallback((text) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'type', text }));
    }
  }, []);

  const handleScroll = useCallback((deltaX, deltaY) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'scroll', deltaX, deltaY }));
    }
  }, []);

  // Resize handling
  const handleResizeStart = useCallback((e) => {
    e.preventDefault();
    setIsResizing(true);
  }, []);

  useEffect(() => {
    const handleMouseMove = (e) => {
      if (isResizing) {
        const newWidth = window.innerWidth - e.clientX;
        setBrowserWidth(Math.max(400, Math.min(newWidth, window.innerWidth - 100)));
      }
    };

    const handleMouseUp = () => {
      setIsResizing(false);
    };

    if (isResizing) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isResizing]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.ctrlKey || e.metaKey) {
        switch (e.key.toLowerCase()) {
          case 't':
            e.preventDefault();
            handleNewTab();
            break;
          case 'w':
            e.preventDefault();
            handleTabClose(activeTabId);
            break;
          case 'r':
            e.preventDefault();
            handleRefresh();
            break;
          default:
            break;
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [activeTabId, handleNewTab, handleTabClose, handleRefresh]);

  if (!isExpanded) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              data-testid="open-virtual-browser-button"
              onClick={() => setIsExpanded(true)}
              className={cn(
                'fixed right-4 top-1/2 -translate-y-1/2 z-50',
                'flex items-center justify-center w-12 h-12 rounded-xl',
                'bg-zinc-800 border border-zinc-700 shadow-2xl',
                'text-zinc-300 hover:text-white hover:bg-zinc-700',
                'transition-all duration-300 hover:scale-105'
              )}
            >
              <Monitor className="w-5 h-5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="left">
            <p>Open Virtual Browser</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return (
    <div
      data-testid="virtual-browser-panel"
      className={cn(
        'fixed right-0 top-0 h-screen z-40 flex',
        'transition-all duration-300 ease-out',
        isFullscreen ? 'left-0' : ''
      )}
      style={{ width: isFullscreen ? '100%' : browserWidth }}
    >
      {/* Resize handle */}
      {!isFullscreen && (
        <div
          ref={resizeRef}
          onMouseDown={handleResizeStart}
          className={cn(
            'w-1 h-full cursor-ew-resize flex items-center justify-center group',
            'hover:bg-sky-500/20 transition-colors',
            isResizing && 'bg-sky-500/30'
          )}
        >
          <div className="w-1 h-20 rounded-full bg-zinc-600 group-hover:bg-sky-400 transition-colors" />
        </div>
      )}

      {/* Main browser container */}
      <div className="flex-1 flex flex-col bg-zinc-900 border-l border-zinc-700/50 shadow-2xl overflow-hidden">
        {/* Window controls */}
        <div className="flex items-center justify-between px-3 py-2 bg-zinc-900 border-b border-zinc-800">
          <div className="flex items-center gap-2">
            <div data-testid="window-close-button" className="w-3 h-3 rounded-full bg-red-500 hover:bg-red-400 cursor-pointer" onClick={onClose} />
            <div data-testid="window-minimize-button" className="w-3 h-3 rounded-full bg-amber-500 hover:bg-amber-400 cursor-pointer" onClick={() => setIsExpanded(false)} />
            <div data-testid="window-fullscreen-toggle" className="w-3 h-3 rounded-full bg-emerald-500 hover:bg-emerald-400 cursor-pointer" onClick={() => setIsFullscreen(!isFullscreen)} />
          </div>
          
          <div className="flex items-center gap-1">
            <span className="text-xs text-zinc-500 mr-2">Virtual Chromium</span>
            {wsConnected && (
              <span data-testid="live-status-indicator" className="flex items-center gap-1 text-xs text-emerald-400 mr-2">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                Live
              </span>
            )}
            
            {/* Extensions toggle */}
            <button
              data-testid="extensions-toggle-button"
              onClick={() => setShowExtensions(!showExtensions)}
              className={cn(
                'p-2 rounded-lg transition-all',
                showExtensions 
                  ? 'bg-sky-500/20 text-sky-400' 
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
              )}
              title="Extensions"
            >
              <Puzzle className="w-4 h-4" />
            </button>
            
            {/* Collapse button */}
            <button
              data-testid="browser-collapse-button"
              onClick={() => setIsExpanded(false)}
              className="p-2 rounded-lg text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-all"
              title="Collapse"
            >
              <PanelRightClose className="w-4 h-4" />
            </button>
            
            {/* Fullscreen toggle */}
            <button
              data-testid="browser-fullscreen-button"
              onClick={() => setIsFullscreen(!isFullscreen)}
              className="p-2 rounded-lg text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-all"
              title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            >
              {isFullscreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
            </button>
          </div>
        </div>

        {/* Tab bar */}
        <TabBar
          tabs={tabs}
          activeTabId={activeTabId}
          onTabActivate={handleTabActivate}
          onTabClose={handleTabClose}
          onNewTab={handleNewTab}
          onTabsReorder={handleTabsReorder}
        />

        {/* Address bar */}
        <AddressBar
          url={activeTab?.url || ''}
          isLoading={isLoading}
          canGoBack={canGoBack}
          canGoForward={canGoForward}
          isSecure={activeTab?.url?.startsWith('https://')}
          onNavigate={handleNavigate}
          onBack={handleBack}
          onForward={handleForward}
          onRefresh={handleRefresh}
          onStop={handleStop}
          onHome={handleHome}
          onBookmark={handleBookmark}
          searchSuggestions={searchSuggestions}
          onSearchInput={handleSearchInput}
        />

        {/* Main content area */}
        <div className="flex-1 flex overflow-hidden">
          {/* Browser viewport */}
          <div className={cn(
            'flex-1 flex flex-col',
            showExtensions && 'border-r border-zinc-700/50'
          )}>
            <BrowserViewport
              url={activeTab?.url}
              isLoading={isLoading}
              screenshot={screenshot}
              error={error}
              onMouseMove={handleMouseMove}
              onMouseClick={handleMouseClick}
              onKeyPress={handleKeyPress}
              onType={handleType}
              onScroll={handleScroll}
              onNavigate={handleNavigate}
              sessionId={sessionId}
              wsConnected={wsConnected}
              onRetry={initSession}
            />
          </div>

          {/* Extensions panel */}
          {showExtensions && (
            <div className="w-80 flex-shrink-0">
              <ExtensionsPanel
                extensions={extensions}
                developerMode={developerMode}
                onToggleDeveloperMode={setDeveloperMode}
                onLoadUnpacked={handleLoadUnpacked}
                onPackExtension={handlePackExtension}
                onToggleExtension={handleToggleExtension}
                onRemoveExtension={handleRemoveExtension}
                onRefresh={handleRefreshExtensions}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default VirtualBrowser;
