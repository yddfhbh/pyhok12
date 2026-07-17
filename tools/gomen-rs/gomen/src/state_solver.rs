use std::collections::HashSet;

use smallvec::SmallVec;

use srs_4l::{
    base64::base64_encode,
    brokenboard::BrokenBoard,
    gameplay::{Board, Physics, Shape},
    vector::Placements,
};

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct SearchStateKey {
    board: BrokenBoard,
    current: Shape,
    hold: Option<Shape>,
    next_index: usize,
    can_hold_now: bool,
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct ExactSolution {
    pub board: BrokenBoard,
    pub placements: SmallVec<[Shape; 10]>,
    pub hold_actions: SmallVec<[bool; 10]>,
    pub final_hold: Option<Shape>,
    pub consumed_next_count: usize,
}

fn should_prune(legal_boards: &HashSet<Board>, board: Board) -> bool {
    if board.has_isolated_cell() || board.has_imbalanced_split() {
        return true;
    }
    !legal_boards.is_empty() && !legal_boards.contains(&board)
}

fn is_filled(board: Board) -> bool {
    board.0.count_ones() == 40
}

fn push_solution(
    results: &mut Vec<ExactSolution>,
    seen: &mut HashSet<(String, String, String, Option<Shape>, usize)>,
    board: BrokenBoard,
    placements: &SmallVec<[Shape; 10]>,
    hold_actions: &SmallVec<[bool; 10]>,
    final_hold: Option<Shape>,
    consumed_next_count: usize,
) {
    let placements_key: String = placements.iter().map(|shape| shape.name()).collect();
    let hold_key: String = hold_actions
        .iter()
        .map(|value| if *value { '1' } else { '0' })
        .collect();
    let key = (
        encode_board(&board),
        placements_key,
        hold_key,
        final_hold,
        consumed_next_count,
    );
    if !seen.insert(key) {
        return;
    }

    results.push(ExactSolution {
        board,
        placements: placements.clone(),
        hold_actions: hold_actions.clone(),
        final_hold,
        consumed_next_count,
    });
}

struct SearchContext<'a> {
    legal_boards: &'a HashSet<Board>,
    next_queue: &'a [Shape],
    physics: Physics,
    dead_states: HashSet<SearchStateKey>,
    seen_solutions: HashSet<(String, String, String, Option<Shape>, usize)>,
    results: Vec<ExactSolution>,
}

impl<'a> SearchContext<'a> {
    fn search(
        &mut self,
        board: BrokenBoard,
        current: Shape,
        hold: Option<Shape>,
        next_index: usize,
        can_hold_now: bool,
        placements: &mut SmallVec<[Shape; 10]>,
        hold_actions: &mut SmallVec<[bool; 10]>,
    ) -> bool {
        let state_key = SearchStateKey {
            board: board.clone(),
            current,
            hold,
            next_index,
            can_hold_now,
        };
        if self.dead_states.contains(&state_key) {
            return false;
        }

        let mut found_any = false;

        for (piece, _) in Placements::place(board.board, current, self.physics).canonical() {
            let new_board = board.place(piece);
            if should_prune(self.legal_boards, new_board.board) {
                continue;
            }

            placements.push(current);
            hold_actions.push(false);

            if is_filled(new_board.board) {
                push_solution(
                    &mut self.results,
                    &mut self.seen_solutions,
                    new_board,
                    placements,
                    hold_actions,
                    hold,
                    next_index,
                );
                found_any = true;
            } else if let Some(&next_current) = self.next_queue.get(next_index) {
                if self.search(
                    new_board,
                    next_current,
                    hold,
                    next_index + 1,
                    true,
                    placements,
                    hold_actions,
                ) {
                    found_any = true;
                }
            }

            placements.pop();
            hold_actions.pop();
        }

        if can_hold_now {
            if let Some(held_piece) = hold {
                for (piece, _) in Placements::place(board.board, held_piece, self.physics).canonical() {
                    let new_board = board.place(piece);
                    if should_prune(self.legal_boards, new_board.board) {
                        continue;
                    }

                    placements.push(held_piece);
                    hold_actions.push(true);

                    if is_filled(new_board.board) {
                        push_solution(
                            &mut self.results,
                            &mut self.seen_solutions,
                            new_board,
                            placements,
                            hold_actions,
                            Some(current),
                            next_index,
                        );
                        found_any = true;
                    } else if let Some(&next_current) = self.next_queue.get(next_index) {
                        if self.search(
                            new_board,
                            next_current,
                            Some(current),
                            next_index + 1,
                            true,
                            placements,
                            hold_actions,
                        ) {
                            found_any = true;
                        }
                    }

                    placements.pop();
                    hold_actions.pop();
                }
            } else if let Some(&held_from_next) = self.next_queue.get(next_index) {
                for (piece, _) in Placements::place(board.board, held_from_next, self.physics).canonical() {
                    let new_board = board.place(piece);
                    if should_prune(self.legal_boards, new_board.board) {
                        continue;
                    }

                    placements.push(held_from_next);
                    hold_actions.push(true);

                    if is_filled(new_board.board) {
                        push_solution(
                            &mut self.results,
                            &mut self.seen_solutions,
                            new_board,
                            placements,
                            hold_actions,
                            Some(current),
                            next_index + 1,
                        );
                        found_any = true;
                    } else if let Some(&next_current) = self.next_queue.get(next_index + 1) {
                        if self.search(
                            new_board,
                            next_current,
                            Some(current),
                            next_index + 2,
                            true,
                            placements,
                            hold_actions,
                        ) {
                            found_any = true;
                        }
                    }

                    placements.pop();
                    hold_actions.pop();
                }
            }
        }

        if !found_any {
            self.dead_states.insert(state_key);
        }
        found_any
    }
}

pub fn compute_exact(
    legal_boards: &HashSet<Board>,
    start: &BrokenBoard,
    current: Shape,
    hold: Option<Shape>,
    next_queue: &[Shape],
    can_hold: bool,
    physics: Physics,
) -> Vec<ExactSolution> {
    let mut context = SearchContext {
        legal_boards,
        next_queue,
        physics,
        dead_states: HashSet::new(),
        seen_solutions: HashSet::new(),
        results: Vec::new(),
    };
    let mut placements = SmallVec::new();
    let mut hold_actions = SmallVec::new();
    let _ = context.search(
        start.clone(),
        current,
        hold,
        0,
        can_hold,
        &mut placements,
        &mut hold_actions,
    );
    context.results.sort_unstable();
    context.results
}

pub fn render_cells(board: &BrokenBoard) -> String {
    let mut rendered = String::new();
    super::solver::print(board, &mut rendered);
    rendered
}

pub fn encode_board(board: &BrokenBoard) -> String {
    let mut encoded = String::new();
    base64_encode(&board.encode(), &mut encoded);
    encoded
}
