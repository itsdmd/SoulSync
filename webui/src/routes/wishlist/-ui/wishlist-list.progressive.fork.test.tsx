/** fork: a very long wishlist draws a window of itself, not every row. */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ParsedWishlistTrack, WishlistArtistGroup } from '../-wishlist.types';

import { WishlistList } from './wishlist-list';

vi.mock('../../../features/downloads/inspector-modal', () => ({
  openWishlistInspector: vi.fn(),
}));

afterEach(cleanup);

function tracks(artist: string, count: number): ParsedWishlistTrack[] {
  return Array.from({ length: count }, (_, i) => ({
    track: `Song ${i}`,
    artist,
    album: 'Single',
    image: '',
    type: 'single' as const,
    id: `${artist}-${i}`,
    retry: 0,
    failing: false,
    lastTried: '',
    failReason: '',
  }));
}

function artist(name: string, count: number): WishlistArtistGroup {
  return { name, albums: [], singles: tracks(name, count), total: count, failingCount: 0 };
}

const noop = () => {};

describe('WishlistList on a long wishlist', () => {
  it('lists the artists a window at a time', () => {
    const groups = Array.from({ length: 100 }, (_, i) => artist(`Artist ${i}`, 1));
    render(
      <WishlistList
        groups={groups}
        artistImages={new Map()}
        onRemoveAlbum={noop}
        onRemoveTrack={noop}
      />,
    );
    expect(document.querySelectorAll('.wl-list-section')).toHaveLength(40);
    fireEvent.click(screen.getByTestId('progressive-more'));
    expect(document.querySelectorAll('.wl-list-section')).toHaveLength(80);
  });

  it('keeps a filter that matches thousands of tracks collapsed, and windows an opened artist', () => {
    const groups = [artist('Big', 500), artist('Other', 500)];
    render(
      <WishlistList
        groups={groups}
        artistImages={new Map()}
        onRemoveAlbum={noop}
        onRemoveTrack={noop}
        filterActive
      />,
    );
    expect(document.querySelectorAll('.wl-list-track')).toHaveLength(0);

    fireEvent.click(document.querySelectorAll('.wl-list-sep')[0] as HTMLElement);
    expect(document.querySelectorAll('.wl-list-track')).toHaveLength(60);
    fireEvent.click(screen.getByTestId('progressive-more'));
    expect(document.querySelectorAll('.wl-list-track')).toHaveLength(120);
  });

  it('still opens every match of a small filter', () => {
    const groups = [artist('Few', 5)];
    render(
      <WishlistList
        groups={groups}
        artistImages={new Map()}
        onRemoveAlbum={noop}
        onRemoveTrack={noop}
        filterActive
      />,
    );
    expect(document.querySelectorAll('.wl-list-track')).toHaveLength(5);
  });
});
