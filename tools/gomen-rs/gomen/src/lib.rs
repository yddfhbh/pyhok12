use js_sys::Uint8Array;
use miniserde::{json, Serialize};
use queue::Bag;
use std::{collections::HashSet, io::Cursor};
use wasm_bindgen::prelude::wasm_bindgen;

use srs_4l::{
    base64::{base64_decode, base64_encode},
    board_list,
    brokenboard::BrokenBoard,
    gameplay::{Board, Physics, Shape},
};

pub mod queue;
pub mod solver;
pub mod state_solver;

#[wasm_bindgen]
pub struct Solver {
    boards: HashSet<Board>,
}

#[wasm_bindgen]
impl Solver {
    #[wasm_bindgen(constructor)]
    pub fn init(legal_boards: Option<Uint8Array>) -> Solver {
        let boards: HashSet<Board> = match legal_boards {
            Some(arr) => board_list::read(Cursor::new(&arr.to_vec()))
                .unwrap()
                .drain(..)
                .collect(),
            None => Default::default(),
        };

        Solver { boards }
    }

    pub fn solve(&self, queue: Queue, garbage: u64, can_hold: bool, physics: String) -> String {
        let empty_boards = Default::default();

        let start = BrokenBoard::from_garbage(garbage);

        let legal_boards = if self.is_fast(garbage) {
            &self.boards
        } else {
            &empty_boards
        };

        let physics = match physics.as_ref() {
            "SRS" => Physics::SRS,
            "Jstris" => Physics::Jstris,
            "TETRIO" => Physics::Tetrio,
            _ => return "".into(),
        };

        let solutions = solver::compute(legal_boards, &start, &queue.bags, can_hold, physics);
        let mut str = String::new();

        for board in &solutions {
            solver::print(&board, &mut str);
            str.push('|');
            base64_encode(&board.encode(), &mut str);
            str.push(',');
        }

        str.pop();
        str
    }

    pub fn solve_state(
        &self,
        next_queue: &str,
        garbage: u64,
        current: char,
        initial_hold: &str,
        can_hold: bool,
        physics: String,
    ) -> String {
        #[derive(Serialize)]
        struct StateSolution {
            cells: String,
            id: String,
            initial_current: String,
            initial_hold: Option<String>,
            initial_next: String,
            placements: Vec<String>,
            hold_actions: Vec<bool>,
            final_hold: Option<String>,
            consumed_next_count: usize,
            physics: String,
        }

        let empty_boards = Default::default();
        let start = BrokenBoard::from_garbage(garbage);
        let legal_boards = if self.is_fast(garbage) {
            &self.boards
        } else {
            &empty_boards
        };
        let physics_enum = match parse_physics(&physics) {
            Some(value) => value,
            None => return "[]".into(),
        };
        let current_shape = match parse_shape(current) {
            Some(value) => value,
            None => return "[]".into(),
        };
        let hold_shape = if initial_hold.trim().is_empty() {
            None
        } else {
            initial_hold.chars().next().and_then(parse_shape)
        };
        let next_shapes = parse_shape_text(next_queue);

        let solutions = state_solver::compute_exact(
            legal_boards,
            &start,
            current_shape,
            hold_shape,
            &next_shapes,
            can_hold,
            physics_enum,
        );

        let serialized: Vec<StateSolution> = solutions
            .into_iter()
            .map(|solution| StateSolution {
                cells: state_solver::render_cells(&solution.board),
                id: state_solver::encode_board(&solution.board),
                initial_current: current_shape.name().to_string(),
                initial_hold: hold_shape.map(|shape| shape.name().to_string()),
                initial_next: next_queue.to_string(),
                placements: solution
                    .placements
                    .iter()
                    .map(|shape| shape.name().to_string())
                    .collect(),
                hold_actions: solution.hold_actions.iter().copied().collect(),
                final_hold: solution.final_hold.map(|shape| shape.name().to_string()),
                consumed_next_count: solution.consumed_next_count,
                physics: physics.clone(),
            })
            .collect();

        json::to_string(&serialized)
    }

    pub fn is_fast(&self, garbage: u64) -> bool {
        self.boards
            .contains(&BrokenBoard::from_garbage(garbage).board)
    }
}

#[wasm_bindgen]
pub struct Queue {
    bags: Vec<Bag>,
}

#[wasm_bindgen]
impl Queue {
    #[wasm_bindgen(constructor)]
    pub fn new() -> Queue {
        Queue { bags: Vec::new() }
    }

    pub fn add_shape(&mut self, shape: char) {
        self.add_bag(&shape.to_string(), 1);
    }

    pub fn add_bag(&mut self, shapes: &str, count: u8) {
        let shapes = shapes
            .chars()
            .map(parse_shape)
            .collect::<Option<Vec<Shape>>>()
            .unwrap();
        self.bags.push(Bag::new(&shapes, count));
    }
}

fn parse_shape(shape: char) -> Option<Shape> {
    match shape {
        'I' => Some(Shape::I),
        'J' => Some(Shape::J),
        'L' => Some(Shape::L),
        'O' => Some(Shape::O),
        'S' => Some(Shape::S),
        'T' => Some(Shape::T),
        'Z' => Some(Shape::Z),
        _ => None,
    }
}

fn parse_shape_text(text: &str) -> Vec<Shape> {
    text.chars().filter_map(parse_shape).collect()
}

fn parse_physics(text: &str) -> Option<Physics> {
    match text {
        "SRS" => Some(Physics::SRS),
        "Jstris" => Some(Physics::Jstris),
        "TETRIO" => Some(Physics::Tetrio),
        _ => None,
    }
}

#[wasm_bindgen]
extern "C" {
    pub fn progress(piece_count: usize, stage: usize, board_idx: usize, board_total: usize);
}

#[wasm_bindgen]
pub fn solution_info(encoded: &str) -> String {
    solution_info_with_physics(encoded, "SRS".into())
}

#[wasm_bindgen]
pub fn solution_info_with_physics(encoded: &str, physics: String) -> String {
    let mut ret = "".to_string();

    let bits = match base64_decode(encoded) {
        Some(b) => b,
        None => return ret,
    };

    let board = match BrokenBoard::decode(&bits) {
        Some(b) => b,
        None => return ret,
    };

    let physics = match parse_physics(&physics) {
        Some(value) => value,
        None => return ret,
    };

    let mut without_hold = board.supporting_queues(physics);
    without_hold.sort_unstable_by_key(|q| q.natural_order_key());

    let with_hold = srs_4l::queue::Queue::unhold_many(&without_hold);

    solver::print(&board, &mut ret);

    ret.push('|');

    for &queue in &without_hold {
        ret.push_str(&queue.to_string());
        ret.push(',');
    }
    if !without_hold.is_empty() {
        ret.pop();
    }

    ret.push('|');

    for &queue in &with_hold {
        ret.push_str(&queue.to_string());
        ret.push(',');
    }
    if !with_hold.is_empty() {
        ret.pop();
    }

    ret
}

#[wasm_bindgen]
pub fn decode_fumen(encoded: &str) -> String {
    #[derive(Default, Serialize)]
    struct Decoded {
        field: u64,
        comment: Option<String>,
    }

    fn inner(encoded: &str) -> Option<Decoded> {
        use fumen::{CellColor, Fumen, Page};

        let fumen = Fumen::decode(encoded).ok()?;
        let page: &Page = fumen.pages.get(0)?;

        if page.field[4..] != [[CellColor::Empty; 10]; 19]
            || page.garbage_row != [CellColor::Empty; 10]
        {
            return None;
        }

        let mut field = 0;
        for idx in 0..40 {
            let cell: CellColor = page.field[idx / 10][idx % 10];
            let filled = cell != CellColor::Empty;
            field |= (filled as u64) << idx;
        }

        let comment = page.comment.clone();
        Some(Decoded { field, comment })
    }

    json::to_string(&inner(encoded))
}
