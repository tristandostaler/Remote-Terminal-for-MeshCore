import { createContext, useContext, type ReactNode } from 'react';

interface ShowRegionContextValue {
  showRegion: boolean;
  setShowRegion: (enabled: boolean) => void;
}

const noop = () => {};

const ShowRegionContext = createContext<ShowRegionContextValue>({
  showRegion: false,
  setShowRegion: noop,
});

export function ShowRegionProvider({
  showRegion,
  setShowRegion,
  children,
}: ShowRegionContextValue & { children: ReactNode }) {
  return (
    <ShowRegionContext.Provider value={{ showRegion, setShowRegion }}>
      {children}
    </ShowRegionContext.Provider>
  );
}

export function useShowRegion() {
  return useContext(ShowRegionContext);
}
